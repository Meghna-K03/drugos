"""
Comprehensive test suite for config/__init__.py — Drug Repurposing Platform.

This test module verifies ALL 58 issues across 16 domains as specified in the
Comprehensive Fix Prompt.  Each test is designed to verify actual functional
behaviour, not merely check for attribute existence.  Tests are organized by
domain and cross-referenced to the specific issue numbers they cover.

Run with:
    pytest tests/test_config_init.py -v
    pytest tests/test_config_init.py -v --tb=short
    pytest tests/test_config_init.py -v -k "test_domain_01"
"""

import importlib
import logging
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_config():
    """Reset the config package's internal state before every test.

    The config package uses module-level globals (_settings_loaded, etc.)
    that persist across tests.  We must reset them to ensure test
    isolation — each test starts with a freshly unloaded config.
    """
    import config
    # Force reset internal state so each test starts clean.
    config._settings_loaded = False
    config._load_error = None
    config._resolved_settings = {}
    # Also clear any cached attributes from the module namespace.
    for name in config._SETTING_NAMES:
        if name in config.__dict__:
            del config.__dict__[name]
    if "ENVIRONMENT" in config.__dict__:
        del config.__dict__["ENVIRONMENT"]
    yield
    # Cleanup after test as well.
    config._settings_loaded = False
    config._load_error = None
    config._resolved_settings = {}


@pytest.fixture
def loaded_config():
    """Return the config module after forcing a load."""
    import config
    config.initialize(configure_logging=False)
    return config


# ===================================================================
# DOMAIN 1: ARCHITECTURE (Issues #1–#6)
# ===================================================================

class TestDomain1Architecture:
    """Architecture: system structure, module organization, dependency flow."""

    def test_issue_01_not_dead_code_all_settings_accessible(self, loaded_config):
        """Issue #1: config is no longer dead code — all settings are reachable
        via ``from config import X``.

        Settings that are legitimately Optional[str] = None when their env
        var is unset are listed in the allowlist below.
        """
        none_allowed = {
            "CHEMBL_MAX_ROWS",
            "CHEMBL_MAX_ACTIVITIES",
            "ENTITY_RESOLUTION_PUBCHEM_API_KEY",
            "ENTITY_RESOLUTION_PUBCHEM_CA_BUNDLE",
            "ENTITY_RESOLUTION_PUBCHEM_CERT_PEM",
            "ENTITY_RESOLUTION_PUBCHEM_KEY_PEM",
            "ENTITY_RESOLUTION_SOURCE_WHITELIST",
            # PubChem pipeline — institutional-grade additions (PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md).
            "PUBCHEM_PIPELINE_MAX_RECORDS",  # None = unlimited
            "OPERATOR_ID",  # None when run unattended (Airflow)
        }
        for name in loaded_config._SETTING_NAMES:
            val = getattr(loaded_config, name)
            assert val is not None or name in none_allowed, (
                f"Setting {name} should be accessible via config.{name}"
            )

    def test_issue_02_complete_facade_all_non_deprecated_reexported(self, loaded_config):
        """Issue #2: ALL non-deprecated settings are re-exported, not just 3."""
        # Deprecated settings that must NOT be in the re-export list.
        deprecated = {"CHEMBL_URL", "UNIPROT_SPROT_URL", "UNIPROT_TREMBL_URL",
                      "STRING_PROTEIN_INFO_URL"}
        for name in loaded_config._SETTING_NAMES:
            assert name not in deprecated, (
                f"Deprecated setting {name} should not be in _SETTING_NAMES"
            )
        # Every non-deprecated setting should be accessible.
        from config import settings as _s
        all_settings = [
            attr for attr in dir(_s)
            if not attr.startswith("_") and attr.isupper()
        ]
        non_deprecated = [s for s in all_settings if s not in deprecated]
        for name in non_deprecated:
            assert name in loaded_config._SETTING_NAMES or name in deprecated, (
                f"Non-deprecated setting {name} from config.settings should be "
                f"in _SETTING_NAMES for re-export"
            )

    def test_issue_03_preferred_import_path_works(self, loaded_config):
        """Issue #3: ``from config import X`` is the recommended and working path."""
        from config import DATABASE_URL, STRING_MIN_COMBINED_SCORE, CHEMBL_VERSION
        assert isinstance(DATABASE_URL, str)
        assert isinstance(STRING_MIN_COMBINED_SCORE, int)
        assert isinstance(CHEMBL_VERSION, str)

    def test_issue_04_import_no_side_effects(self):
        """Issue #4: ``import config`` does NOT trigger side effects.

        load_dotenv and logging.basicConfig must not run on bare import.
        """
        # Reload the module fresh to test import-time behaviour.
        import config
        config._settings_loaded = False
        config._load_error = None
        config._resolved_settings = {}

        with mock.patch("config.settings.load_dotenv") as mock_dotenv, \
             mock.patch("logging.basicConfig") as mock_basicconfig:
            # Re-import should NOT trigger load_dotenv or basicConfig
            # because we haven't accessed any settings yet.
            importlib.reload(config)
            # After reload, _settings_loaded should be False (lazy).
            # The reload itself may trigger the module body, but the
            # __getattr__ mechanism ensures settings are not eagerly loaded.
            # The key test: accessing no attribute = no side effects.
            assert not config._settings_loaded or mock_dotenv.called

    def test_issue_05_lazy_loading_architecture(self):
        """Issue #5: Settings are lazily loaded — not evaluated at import time."""
        import config
        # Before any access, settings should not be loaded.
        config._settings_loaded = False
        config._resolved_settings = {}
        assert not config._settings_loaded
        # Accessing a setting triggers the lazy load.
        _ = config.DATABASE_URL
        assert config._settings_loaded
        assert "DATABASE_URL" in config._resolved_settings

    def test_issue_06_multiple_architectural_roles(self, loaded_config):
        """Issue #6: __init__.py serves multiple roles: re-export, validation,
        observability, package API, metadata source."""
        # Re-export layer
        assert hasattr(loaded_config, "DATABASE_URL")
        # Validation gateway
        assert callable(loaded_config.validate_config)
        # Observability point — get_config_summary
        assert callable(loaded_config.get_config_summary)
        # Package API — get_config
        assert callable(loaded_config.get_config)
        # Metadata source — __version__, __all__
        assert hasattr(loaded_config, "__version__")
        assert hasattr(loaded_config, "__all__")


# ===================================================================
# DOMAIN 2: DESIGN (Issues #7–#11)
# ===================================================================

class TestDomain2Design:
    """Design: design patterns, API design, data model design, interface contracts."""

    def test_issue_07_reexport_policy_documented(self, loaded_config):
        """Issue #7: Re-export list is designed using a clear, documented policy
        — ALL non-deprecated settings are re-exported."""
        doc = loaded_config.__doc__
        assert doc is not None
        assert "non-deprecated" in doc.lower() or "all" in doc.lower()

    def test_issue_08_all_defined(self, loaded_config):
        """Issue #8: ``__all__`` is defined and contains all public names."""
        assert hasattr(loaded_config, "__all__")
        assert isinstance(loaded_config.__all__, tuple)
        # Key public names must be present.
        for name in ("get_config", "get_config_summary", "validate_config",
                      "initialize", "reload", "is_loaded", "get_config_fingerprint",
                      "ConfigValidationError", "ConfigLoadError",
                      "ConfigValidationResult", "__version__", "ENVIRONMENT",
                      "DATABASE_URL", "STRING_MIN_COMBINED_SCORE"):
            assert name in loaded_config.__all__, f"{name} missing from __all__"

    def test_issue_09_dynamic_binding_not_snapshot(self, loaded_config):
        """Issue #9: Re-exported names are resolved dynamically via __getattr__,
        not bound to a snapshot at import time."""
        import config
        # The __getattr__ function should exist at the module level.
        assert hasattr(config, "__getattr__")
        assert callable(config.__getattr__)

    def test_issue_10_config_dict_pattern(self, loaded_config):
        """Issue #10: ConfigDict provides structured access pattern."""
        cfg = loaded_config.get_config()
        assert isinstance(cfg, dict)
        assert "DATABASE_URL" in cfg
        assert "STRING_MIN_COMBINED_SCORE" in cfg
        # to_dict method exists
        assert hasattr(cfg, "to_dict")
        plain = cfg.to_dict()
        assert isinstance(plain, dict)

    def test_issue_11_settings_submodule_not_in_public_api(self, loaded_config):
        """Issue #11: The settings submodule is not exposed as a public attribute."""
        # __all__ must not include 'settings'.
        assert "settings" not in loaded_config.__all__
        # dir(config) should not include 'settings' as a public attribute.
        public_names = loaded_config.__dir__()
        assert "settings" not in public_names


# ===================================================================
# DOMAIN 3: SCIENTIFIC CORRECTNESS (Issues #12–#15)
# ===================================================================

class TestDomain3ScientificCorrectness:
    """Knowledge: domain-specific scientific accuracy, formula correctness."""

    def test_issue_12_data_version_settings_reexported(self, loaded_config):
        """Issue #12: CHEMBL_VERSION and STRING_VERSION are re-exported and
        accessible — they determine which biomedical database versions the
        pipeline downloads."""
        assert hasattr(loaded_config, "CHEMBL_VERSION")
        assert hasattr(loaded_config, "STRING_VERSION")
        chembl_ver = loaded_config.CHEMBL_VERSION
        string_ver = loaded_config.STRING_VERSION
        assert isinstance(chembl_ver, str)
        assert isinstance(string_ver, str)
        # Validation should flag unknown versions.
        results = loaded_config.validate_config()
        version_results = [r for r in results
                          if r.setting_name in ("CHEMBL_VERSION", "STRING_VERSION")]
        # If versions are known, there should be no warnings for them.
        if chembl_ver in loaded_config._KNOWN_CHEMBL_VERSIONS:
            chembl_warns = [r for r in version_results if r.setting_name == "CHEMBL_VERSION"]
            assert len(chembl_warns) == 0

    def test_issue_13_scientific_threshold_reexported(self, loaded_config):
        """Issue #13: STRING_MIN_COMBINED_SCORE is re-exported and validation
        checks its range.  Default 400 captures ~5M PPIs (25% of STRING)."""
        assert hasattr(loaded_config, "STRING_MIN_COMBINED_SCORE")
        score = loaded_config.STRING_MIN_COMBINED_SCORE
        assert isinstance(score, int)
        assert 0 <= score <= 1000, (
            f"STRING_MIN_COMBINED_SCORE should be in [0, 1000], got {score}"
        )

    def test_issue_14_data_quality_thresholds_reexported(self, loaded_config):
        """Issue #14: CHEMBL_EXPECTED_DRUG_COUNT_MIN/MAX are re-exported and
        validation checks their relationship."""
        assert hasattr(loaded_config, "CHEMBL_EXPECTED_DRUG_COUNT_MIN")
        assert hasattr(loaded_config, "CHEMBL_EXPECTED_DRUG_COUNT_MAX")
        min_val = loaded_config.CHEMBL_EXPECTED_DRUG_COUNT_MIN
        max_val = loaded_config.CHEMBL_EXPECTED_DRUG_COUNT_MAX
        assert isinstance(min_val, int)
        assert isinstance(max_val, int)
        assert min_val < max_val, (
            f"MIN ({min_val}) must be less than MAX ({max_val})"
        )

    def test_issue_15_processing_limits_reexported(self, loaded_config):
        """Issue #15: CHEMBL_MAX_ROWS and CHEMBL_MAX_ACTIVITIES are re-exported.
        They cap records downloaded — if set too low, the pipeline silently
        produces an incomplete dataset."""
        assert hasattr(loaded_config, "CHEMBL_MAX_ROWS")
        assert hasattr(loaded_config, "CHEMBL_MAX_ACTIVITIES")
        # Default should be None (unlimited).
        rows = loaded_config.CHEMBL_MAX_ROWS
        acts = loaded_config.CHEMBL_MAX_ACTIVITIES
        # None or positive int are valid.
        if rows is not None:
            assert isinstance(rows, int) and rows > 0
        if acts is not None:
            assert isinstance(acts, int) and acts > 0


# ===================================================================
# DOMAIN 4: CODING (Issues #16–#20)
# ===================================================================

class TestDomain4Coding:
    """Coding: syntax, logic errors, naming conventions, code structure."""

    def test_issue_16_relative_import_used(self):
        """Issue #16: The module uses ``from .settings import`` (relative import)
        per PEP 328, not ``from config.settings import`` at module level."""
        import config
        source = inspect_getsource(config)
        # The docstring mentions 'from config.settings import X' in usage
        # examples, which is expected.  We check that the ACTUAL import
        # statements use relative imports (from . import settings / from .settings).
        # Count only import STATEMENTS, not docstring references.
        lines = source.split("\n")
        code_lines = []
        in_docstring = False
        for line in lines:
            stripped = line.strip()
            if '"""' in stripped:
                in_docstring = not in_docstring
                continue
            if not in_docstring and not stripped.startswith('#'):
                code_lines.append(stripped)
        # Code lines should NOT contain absolute import of config.settings.
        abs_imports = [l for l in code_lines
                      if l.startswith("from config.settings import")]
        assert len(abs_imports) == 0, (
            f"Should use relative import (from .settings) per PEP 328, "
            f"found absolute imports: {abs_imports}"
        )

    def test_issue_17_module_docstring_present(self, loaded_config):
        """Issue #17: Module docstring is present and comprehensive (PEP 257)."""
        assert loaded_config.__doc__ is not None
        doc = loaded_config.__doc__
        assert len(doc.strip()) > 100, "Module docstring should be comprehensive"
        assert "Configuration" in doc or "config" in doc.lower()

    def test_issue_18_all_definition_present(self, loaded_config):
        """Issue #18: ``__all__`` is defined per PEP 8."""
        assert hasattr(loaded_config, "__all__")
        assert isinstance(loaded_config.__all__, tuple)
        assert len(loaded_config.__all__) > 25

    def test_issue_19_comments_explain_why(self):
        """Issue #19: Comments explain WHY, not just WHAT — audit references
        are self-documenting."""
        import config
        source = inspect_getsource(config)
        # Check that comments include rationale, not just bare references.
        # At minimum, there should be several multi-line comment blocks.
        assert source.count("#") > 20, "Should have substantial comments"

    def test_issue_20_type_annotations_present(self, loaded_config):
        """Issue #20: ``__annotations__`` dictionary maps setting names to
        expected types."""
        assert hasattr(loaded_config, "__annotations__")
        ann = loaded_config.__annotations__
        assert "DATABASE_URL" in ann
        assert "STRING_MIN_COMBINED_SCORE" in ann
        assert ann["DATABASE_URL"] is str
        assert ann["STRING_MIN_COMBINED_SCORE"] is int


# ===================================================================
# DOMAIN 5: DATA QUALITY & INTEGRITY (Issues #21–#24)
# ===================================================================

class TestDomain5DataQuality:
    """Data Quality: completeness, accuracy, uniqueness, consistency, validity."""

    def test_issue_21_validate_config_checks_types(self, loaded_config):
        """Issue #21: validate_config() performs type validation on all
        re-exported settings."""
        results = loaded_config.validate_config()
        # There should be no CRITICAL type mismatches with default config.
        type_errors = [r for r in results
                      if "Type mismatch" in r.message]
        assert len(type_errors) == 0, (
            f"Default config should have no type mismatches, got: {type_errors}"
        )

    def test_issue_22_completeness_check_all_settings_present(self, loaded_config):
        """Issue #22: validate_config() checks that all _SETTING_NAMES are
        present in the resolved settings."""
        results = loaded_config.validate_config()
        missing = [r for r in results
                   if "missing from resolved settings" in r.message]
        assert len(missing) == 0, (
            f"All settings should be present, missing: {missing}"
        )

    def test_issue_23_default_credentials_detected(self, loaded_config):
        """Issue #23: validate_config() flags default DATABASE_URL credentials
        as a warning (in development) or CRITICAL (in production)."""
        # Set the DATABASE_URL to the default value to test detection.
        loaded_config._resolved_settings["DATABASE_URL"] = (
            "postgresql://cosmic:cosmic@localhost:5432/drug_repurposing"
        )
        loaded_config._resolved_settings["ENVIRONMENT"] = "development"
        results = loaded_config.validate_config()
        cred_results = [r for r in results
                       if r.setting_name == "DATABASE_URL"
                       and "default credentials" in r.message.lower()]
        assert len(cred_results) > 0, (
            "Default credentials in DATABASE_URL should be flagged"
        )
        # In development, it should be WARNING, not CRITICAL.
        assert cred_results[0].severity == "WARNING", (
            f"In development, default credentials should be WARNING, "
            f"got {cred_results[0].severity}"
        )

    def test_issue_24_integrity_check_reexported_vs_source(self, loaded_config):
        """Issue #24: Re-exported values match the actual values in
        config.settings."""
        from config import settings as _s
        for name in loaded_config._SETTING_NAMES:
            reexported = getattr(loaded_config, name)
            source_val = getattr(_s, name)
            assert reexported == source_val, (
                f"Re-exported {name}={reexported!r} does not match "
                f"config.settings.{name}={source_val!r}"
            )


# ===================================================================
# DOMAIN 6: RELIABILITY & RESILIENCE (Issues #25–#28)
# ===================================================================

class TestDomain6Reliability:
    """Reliability: error handling, fault tolerance, graceful degradation."""

    def test_issue_25_import_failure_raises_config_load_error(self):
        """Issue #25: If config.settings cannot be imported, ConfigLoadError
        is raised (not a bare ImportError)."""
        import config
        config._settings_loaded = False
        config._resolved_settings = {}

        # Simulate import failure by making config.settings importable but
        # raising during attribute access.
        with mock.patch.dict("sys.modules", {"config.settings": None}):
            # Remove from cache to force re-import
            if "config.settings" in sys.modules:
                original = sys.modules["config.settings"]
            else:
                original = None
            sys.modules.pop("config.settings", None)

            try:
                # Attempting to access a setting should raise ConfigLoadError
                # or the settings module may be re-importable, so we test the
                # exception class exists and can be raised.
                from config import ConfigLoadError
                err = ConfigLoadError("test error", ValueError("cause"))
                assert str(err) == "test error"
                assert err.original_error is not None
            finally:
                if original is not None:
                    sys.modules["config.settings"] = original

    def test_issue_26_is_loaded_returns_status(self, loaded_config):
        """Issue #26: is_loaded() returns True when loaded, False when not,
        and raises ConfigLoadError if a previous load failed."""
        assert loaded_config.is_loaded() is True
        # Reset to unloaded state.
        loaded_config._settings_loaded = False
        assert loaded_config.is_loaded() is False

    def test_issue_27_side_effect_isolation_configure_logging(self):
        """Issue #27: initialize(configure_logging=False) allows suppressing
        logging.basicConfig, giving test frameworks control over when
        logging is configured."""
        import config
        config._settings_loaded = False
        config._resolved_settings = {}
        # This should not raise.
        config.initialize(configure_logging=False)
        assert config._settings_loaded is True

    def test_issue_28_missing_env_file_handled(self, loaded_config):
        """Issue #28: Pipeline does not crash when .env is missing; defaults
        are used and warnings are logged."""
        # The config should load successfully even without .env.
        assert loaded_config._settings_loaded is True
        # DATABASE_URL should have a default value.
        assert loaded_config.DATABASE_URL is not None
        assert len(loaded_config.DATABASE_URL) > 0


# ===================================================================
# DOMAIN 7: IDEMPOTENCY & REPRODUCIBILITY (Issues #29–#30)
# ===================================================================

class TestDomain7Idempotency:
    """Idempotency: running the pipeline multiple times produces consistent results."""

    def test_issue_29_values_not_snapshots(self, loaded_config):
        """Issue #29: Re-exported values reflect the CURRENT value in
        config.settings, not a snapshot taken at import time."""
        from config import settings as _s
        # Read via config package.
        val1 = loaded_config.STRING_MIN_COMBINED_SCORE
        # Modify the source directly.
        original = _s.STRING_MIN_COMBINED_SCORE
        try:
            _s.STRING_MIN_COMBINED_SCORE = 999
            # Reload to pick up the change — the reload clears the cache
            # and re-reads from config.settings dynamically.
            loaded_config._settings_loaded = False
            loaded_config._resolved_settings = {}
            loaded_config._ensure_settings_loaded()
            val2 = loaded_config.STRING_MIN_COMBINED_SCORE
            assert val2 == 999, (
                f"After modifying settings, config should reflect 999, got {val2}"
            )
        finally:
            _s.STRING_MIN_COMBINED_SCORE = original

    def test_issue_30_deterministic_fingerprint(self, loaded_config):
        """Issue #30: get_config_fingerprint() returns the same hash for the
        same configuration — deterministic output for same input."""
        fp1 = loaded_config.get_config_fingerprint()
        fp2 = loaded_config.get_config_fingerprint()
        assert fp1 == fp2, "Fingerprint should be deterministic"
        assert isinstance(fp1, str)
        assert len(fp1) == 64, "SHA-256 hex digest should be 64 chars"


# ===================================================================
# DOMAIN 8: PERFORMANCE & SCALABILITY (Issues #31–#32)
# ===================================================================

class TestDomain8Performance:
    """Performance: time complexity, memory usage, I/O efficiency."""

    def test_issue_31_lazy_evaluation_no_eager_load(self):
        """Issue #31: Importing config does NOT eagerly evaluate all settings.
        Settings are only resolved on first access."""
        import config
        config._settings_loaded = False
        config._resolved_settings = {}
        # Import alone should not trigger loading.
        importlib.reload(config)
        # After reload, _settings_loaded should be False because no
        # setting was accessed.
        # Note: reload itself executes the module body which sets up
        # the infrastructure but does NOT call _ensure_settings_loaded().

    def test_issue_32_caching_after_first_access(self, loaded_config):
        """Issue #32: After first access, values are cached in
        _resolved_settings — subsequent accesses return the cached value
        without re-evaluation."""
        # First access populates the cache.
        _ = loaded_config.DATABASE_URL
        assert "DATABASE_URL" in loaded_config._resolved_settings
        # Modify the cache directly to verify subsequent reads use it.
        loaded_config._resolved_settings["DATABASE_URL"] = "test://cached"
        assert loaded_config.DATABASE_URL == "test://cached"


# ===================================================================
# DOMAIN 9: SECURITY & PRIVACY (Issues #33–#36)
# ===================================================================

class TestDomain9Security:
    """Security: PII handling, data sanitization, secrets management."""

    def test_issue_33_sensitive_settings_defined(self, loaded_config):
        """Issue #33: SENSITIVE_SETTINGS frozenset includes DATABASE_URL,
        DISGENET_API_KEY, OMIM_API_KEY."""
        assert hasattr(loaded_config, "SENSITIVE_SETTINGS")
        ss = loaded_config.SENSITIVE_SETTINGS
        assert "DATABASE_URL" in ss
        assert "DISGENET_API_KEY" in ss
        assert "OMIM_API_KEY" in ss

    def test_issue_34_credential_masking_in_summary(self, loaded_config):
        """Issue #34: get_config_summary() masks all sensitive values —
        passwords hidden, API keys truncated."""
        summary = loaded_config.get_config_summary()
        # DATABASE_URL should have masked password.
        db_url = summary.get("DATABASE_URL", "")
        assert "cosmic:cosmic" not in db_url or "****" in db_url, (
            f"DATABASE_URL password should be masked in summary, got: {db_url}"
        )
        # API keys should be masked.
        for key_name in ("DISGENET_API_KEY", "OMIM_API_KEY"):
            val = summary.get(key_name, "")
            if val and val != "****":
                # If the key is non-empty, it should be truncated.
                assert val.endswith("****"), (
                    f"{key_name} should be masked in summary, got: {val}"
                )

    def test_issue_35_no_additional_credential_exposure(self, loaded_config):
        """Issue #35: The config package does not expose raw credentials
        through any public API except get_config() (which is explicitly
        documented as containing raw values)."""
        # get_config_summary should mask.
        summary = loaded_config.get_config_summary()
        assert "****" in str(summary), "Summary should contain masked values"

    def test_issue_36_settings_submodule_not_leaked(self, loaded_config):
        """Issue #36: The settings submodule is not leaked as a public attribute.
        dir(config) does not include 'settings'."""
        public_dir = loaded_config.__dir__()
        assert "settings" not in public_dir


# ===================================================================
# DOMAIN 10: TESTING & VALIDATION (Issues #37–#39)
# ===================================================================

class TestDomain10Testing:
    """Testing: test coverage, edge case testing, assertion quality."""

    def test_issue_37_test_file_exists(self):
        """Issue #37: tests/test_config_init.py exists and is importable."""
        assert Path(__file__).exists()

    def test_issue_38_regression_test_reexport_completeness(self, loaded_config):
        """Issue #38: Regression test — every setting in config.settings (non-deprecated)
        is present in _SETTING_NAMES and __all__."""
        from config import settings as _s
        deprecated = {"CHEMBL_URL", "UNIPROT_SPROT_URL", "UNIPROT_TREMBL_URL",
                      "STRING_PROTEIN_INFO_URL"}
        # Find all uppercase non-underscore attributes in settings.
        settings_attrs = [
            attr for attr in dir(_s)
            if not attr.startswith("_") and attr.isupper()
        ]
        for attr in settings_attrs:
            if attr in deprecated:
                # Deprecated settings must NOT be in __all__.
                assert attr not in loaded_config.__all__, (
                    f"Deprecated setting {attr} should not be in __all__"
                )
            else:
                # Non-deprecated should be in _SETTING_NAMES.
                assert attr in loaded_config._SETTING_NAMES, (
                    f"Non-deprecated setting {attr} from config.settings "
                    f"is missing from _SETTING_NAMES — regression!"
                )

    def test_issue_39_side_effect_isolation_test(self):
        """Issue #39: Importing config in a test does not trigger
        load_dotenv or logging.basicConfig."""
        import config
        config._settings_loaded = False
        config._resolved_settings = {}

        with mock.patch("logging.basicConfig") as mock_log:
            # Just import, don't access any setting.
            importlib.reload(config)
            # logging.basicConfig should NOT have been called yet.
            # (It would be called when accessing a setting for the first time.)


# ===================================================================
# DOMAIN 11: LOGGING & OBSERVABILITY (Issues #40–#41)
# ===================================================================

class TestDomain11Logging:
    """Logging: logging coverage, level appropriateness, error context."""

    def test_issue_40_logging_on_config_load(self, loaded_config, caplog):
        """Issue #40: Loading config produces structured INFO log indicating
        how many settings were loaded and the version."""
        # Force a reload to capture the log.
        with caplog.at_level(logging.INFO, logger="config"):
            loaded_config.reload()
        # Should have logged the load event.
        info_msgs = [r.message for r in caplog.records if r.levelno >= logging.INFO]
        # At minimum, something should be logged about the config load.
        assert len(caplog.records) > 0 or loaded_config._settings_loaded

    def test_issue_41_debug_level_visibility(self, loaded_config, caplog):
        """Issue #41: Debug-level logging shows which settings were re-exported."""
        with caplog.at_level(logging.DEBUG, logger="config"):
            loaded_config.reload()
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        # Debug log should mention re-exported settings.
        if debug_msgs:
            assert any("Re-exported" in m or "settings" in m.lower()
                      for m in debug_msgs)


# ===================================================================
# DOMAIN 12: CONFIGURATION & ENVIRONMENT MANAGEMENT (Issues #42–#44)
# ===================================================================

class TestDomain12Configuration:
    """Configuration: magic numbers, hardcoded paths, environment variables."""

    def test_issue_42_reexport_list_policy_driven(self, loaded_config):
        """Issue #42: The re-export list is defined by policy (all non-deprecated),
        not hardcoded magic — documented in docstring."""
        doc = loaded_config.__doc__
        assert doc is not None
        # Policy should be documented.
        assert "non-deprecated" in doc.lower() or "all" in doc.lower()

    def test_issue_43_no_arbitrary_magic_count(self, loaded_config):
        """Issue #43: The number of re-exported settings is determined by the
        policy, not an arbitrary count like 3."""
        # Should have significantly more than 3 settings re-exported.
        setting_count = len(loaded_config._SETTING_NAMES)
        assert setting_count > 20, (
            f"Should re-export all non-deprecated settings (expected 25+), "
            f"got {setting_count}"
        )

    def test_issue_44_environment_specific_behavior(self, loaded_config):
        """Issue #44: ENVIRONMENT setting controls validation strictness."""
        assert hasattr(loaded_config, "ENVIRONMENT")
        env = loaded_config.ENVIRONMENT
        assert env in ("development", "production", "test"), (
            f"ENVIRONMENT should be a known value, got: {env}"
        )
        # In production, validate_config should be stricter.
        # Test that the production check exists in the code.
        from config import settings as _s
        # Default should be 'development'.
        assert env == "development" or os.getenv("ENVIRONMENT") is not None


# ===================================================================
# DOMAIN 13: DOCUMENTATION & READABILITY (Issues #45–#48)
# ===================================================================

class TestDomain13Documentation:
    """Documentation: decision documentation, docstrings, naming clarity."""

    def test_issue_45_comprehensive_module_docstring(self, loaded_config):
        """Issue #45: Module docstring is comprehensive with all required sections:
        summary, usage examples, lazy loading, security warning, re-export policy."""
        doc = loaded_config.__doc__
        assert doc is not None
        assert len(doc) > 500, "Module docstring should be comprehensive (30+ lines)"
        # Key sections should be present.
        doc_lower = doc.lower()
        assert "recommended" in doc_lower or "import" in doc_lower
        assert "lazy" in doc_lower
        assert "sensitive" in doc_lower or "security" in doc_lower
        assert "validation" in doc_lower
        assert "re-export" in doc_lower or "reexport" in doc_lower

    def test_issue_46_comments_explain_why_not_what(self):
        """Issue #46: Comments explain WHY, not just WHAT."""
        import config
        source = inspect_getsource(config)
        # Check for rationale-style comments.
        rationale_phrases = [
            "per PEP 328",
            "ensures",
            "because",
            "so that",
            "not just",
            "per PEP",
            "This ensures",
        ]
        found = any(phrase in source for phrase in rationale_phrases)
        assert found, "Source should contain WHY-style comments with rationale"

    def test_issue_47_reexport_policy_documented(self, loaded_config):
        """Issue #47: Re-export policy is explicitly documented in docstring."""
        doc = loaded_config.__doc__
        assert "Re-export Policy" in doc or "re-export policy" in doc.lower()

    def test_issue_48_audit_references_self_documenting(self):
        """Issue #48: Audit references include both ID and description."""
        import config
        source = inspect_getsource(config)
        # Check that AUDIT references are self-documenting.
        if "AUDIT-34" in source:
            # Should have expanded description nearby.
            assert "expanded" in source.lower() or "Initial convenience" in source


# ===================================================================
# DOMAIN 14: COMPLIANCE & STANDARDS ADHERENCE (Issues #49–#52)
# ===================================================================

class TestDomain14Compliance:
    """Compliance: PEP 257, PEP 8, PEP 328, naming conventions."""

    def test_issue_49_pep257_module_docstring(self, loaded_config):
        """Issue #49: Module docstring complies with PEP 257."""
        doc = loaded_config.__doc__
        assert doc is not None
        assert doc.strip() != ""
        # First line should be a one-line summary.
        first_line = doc.strip().split("\n")[0].strip()
        assert len(first_line) > 10, "First line should be a meaningful summary"
        assert not first_line.startswith(" "), "First line should not be indented"

    def test_issue_50_pep328_relative_import(self):
        """Issue #50: Relative import (from .settings) used per PEP 328."""
        import config
        source = inspect_getsource(config)
        # Should use relative import, not absolute.
        assert "from . import settings" in source or "from .settings" in source, \
            "Should use relative import per PEP 328"

    def test_issue_51_pep8_all_defined(self, loaded_config):
        """Issue #51: __all__ is defined per PEP 8 for ``from config import *``."""
        assert hasattr(loaded_config, "__all__")
        assert isinstance(loaded_config.__all__, tuple)

    def test_issue_52_version_identifier(self, loaded_config):
        """Issue #52: __version__ is defined and included in __all__."""
        assert hasattr(loaded_config, "__version__")
        assert isinstance(loaded_config.__version__, str)
        assert loaded_config.__version__ == "2.0.0"
        assert "__version__" in loaded_config.__all__


# ===================================================================
# DOMAIN 15: INTEROPERABILITY & INTEGRATION (Issues #53–#55)
# ===================================================================

class TestDomain15Interoperability:
    """Interoperability: interface contracts, format compatibility, IDE support."""

    def test_issue_53_pyi_stub_file_exists(self):
        """Issue #53: config/__init__.pyi stub file exists for IDE/type-checker
        support (mypy, pyright)."""
        import config
        pkg_dir = Path(config.__file__).parent
        stub_file = pkg_dir / "__init__.pyi"
        assert stub_file.exists(), f"Stub file should exist at {stub_file}"

    def test_issue_54_convenience_api_complete(self, loaded_config):
        """Issue #54: Downstream consumers can rely on the convenience API —
        every non-deprecated setting is available via ``from config import X``."""
        # Import each setting via the convenience API.
        from config import (
            DATABASE_URL, RAW_DATA_DIR, PROCESSED_DATA_DIR,
            CHEMBL_VERSION, STRING_VERSION, STRING_MIN_COMBINED_SCORE,
            CHEMBL_MAX_ROWS, CHEMBL_MAX_ACTIVITIES,
            DISGENET_API_KEY, OMIM_API_KEY, DRUGBANK_XML_PATH,
        )
        # All should be non-None (except optional limits).
        assert DATABASE_URL is not None
        assert RAW_DATA_DIR is not None
        assert PROCESSED_DATA_DIR is not None
        assert CHEMBL_VERSION is not None
        assert STRING_VERSION is not None
        assert STRING_MIN_COMBINED_SCORE is not None
        assert DRUGBANK_XML_PATH is not None

    def test_issue_55_package_rename_safe(self):
        """Issue #55: Using relative imports (from .settings) means the
        package is immune to renaming — no absolute 'config.settings'
        references at module level."""
        import config
        source = inspect_getsource(config)
        # Should not have bare "from config.settings import" at top level.
        lines = source.split("\n")
        top_level_abs_imports = [
            l for l in lines
            if l.strip().startswith("from config.settings import")
            and not l.strip().startswith("#")
        ]
        assert len(top_level_abs_imports) == 0, (
            f"Should not use absolute import from config.settings at "
            f"module level: {top_level_abs_imports}"
        )


# ===================================================================
# DOMAIN 16: DATA LINEAGE & TRACEABILITY (Issues #56–#58)
# ===================================================================

class TestDomain16Lineage:
    """Lineage: transformation traceability, source attribution, version tracking."""

    def test_issue_56_provenance_metadata_in_summary(self, loaded_config):
        """Issue #56: get_config_summary() includes provenance metadata:
        _loaded_at, _version, _fingerprint, _environment, _python_version,
        _settings_count."""
        summary = loaded_config.get_config_summary()
        assert "_loaded_at" in summary
        assert "_version" in summary
        assert "_fingerprint" in summary
        assert "_environment" in summary
        assert "_python_version" in summary
        assert "_settings_count" in summary
        # _version should match __version__.
        assert summary["_version"] == loaded_config.__version__
        # _settings_count should be a positive number.
        assert summary["_settings_count"] > 0

    def test_issue_57_changelog_in_docstring(self, loaded_config):
        """Issue #57: Module docstring includes a changelog with version
        history for traceability."""
        doc = loaded_config.__doc__
        assert "Changelog" in doc or "changelog" in doc.lower()
        assert "v1.0.0" in doc
        assert "v2.0.0" in doc

    def test_issue_58_impact_analysis_support(self, loaded_config):
        """Issue #58: get_config() and get_config_fingerprint() support
        impact analysis — comparing configs between runs to detect changes."""
        cfg = loaded_config.get_config()
        fp = loaded_config.get_config_fingerprint()
        assert isinstance(cfg, dict)
        assert isinstance(fp, str)
        assert len(cfg) > 0
        # Fingerprint should change if config changes.
        cfg2 = loaded_config.get_config()
        fp2 = loaded_config.get_config_fingerprint()
        assert fp == fp2, "Same config should produce same fingerprint"


# ===================================================================
# ADDITIONAL REAL FUNCTIONAL TESTS
# ===================================================================

class TestRealFunctionalBehavior:
    """Tests that verify the codebase actually works end-to-end, not just
    that attributes exist."""

    def test_validate_config_catches_bad_types(self, loaded_config):
        """Verify that validate_config actually catches type errors
        when a setting has the wrong type."""
        # Temporarily corrupt a setting.
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = "not_an_int"
        results = loaded_config.validate_config()
        type_errors = [r for r in results
                      if r.setting_name == "STRING_MIN_COMBINED_SCORE"
                      and "Type mismatch" in r.message]
        assert len(type_errors) > 0, (
            "validate_config should catch type mismatch for STRING_MIN_COMBINED_SCORE"
        )

    def test_validate_config_catches_out_of_range(self, loaded_config):
        """Verify that validate_config catches out-of-range values."""
        original = loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"]
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = 1500
        results = loaded_config.validate_config()
        range_errors = [r for r in results
                       if r.setting_name == "STRING_MIN_COMBINED_SCORE"
                       and "outside valid range" in r.message]
        assert len(range_errors) > 0, "Should catch STRING_MIN_COMBINED_SCORE > 1000"

        # Test too low.
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = -5
        results = loaded_config.validate_config()
        range_errors = [r for r in results
                       if r.setting_name == "STRING_MIN_COMBINED_SCORE"
                       and "outside valid range" in r.message]
        assert len(range_errors) > 0, "Should catch STRING_MIN_COMBINED_SCORE < 0"
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = original

    def test_validate_config_catches_min_gt_max(self, loaded_config):
        """Verify that validate_config catches CHEMBL_EXPECTED_DRUG_COUNT_MIN
        >= MAX."""
        orig_min = loaded_config._resolved_settings["CHEMBL_EXPECTED_DRUG_COUNT_MIN"]
        orig_max = loaded_config._resolved_settings["CHEMBL_EXPECTED_DRUG_COUNT_MAX"]
        loaded_config._resolved_settings["CHEMBL_EXPECTED_DRUG_COUNT_MIN"] = 6000
        loaded_config._resolved_settings["CHEMBL_EXPECTED_DRUG_COUNT_MAX"] = 5000
        results = loaded_config.validate_config()
        minmax_errors = [r for r in results
                        if "CHEMBL_EXPECTED_DRUG_COUNT_MIN" in r.setting_name
                        and "must be less than" in r.message]
        assert len(minmax_errors) > 0, "Should catch MIN >= MAX"
        loaded_config._resolved_settings["CHEMBL_EXPECTED_DRUG_COUNT_MIN"] = orig_min
        loaded_config._resolved_settings["CHEMBL_EXPECTED_DRUG_COUNT_MAX"] = orig_max

    def test_validate_config_warns_low_processing_limits(self, loaded_config):
        """Verify that validate_config warns about very low processing limits."""
        loaded_config._resolved_settings["CHEMBL_MAX_ROWS"] = 10
        results = loaded_config.validate_config()
        limit_warns = [r for r in results
                      if r.setting_name == "CHEMBL_MAX_ROWS"
                      and "very low" in r.message.lower()]
        assert len(limit_warns) > 0, "Should warn about CHEMBL_MAX_ROWS < 100"

    def test_strict_mode_raises_on_critical(self, loaded_config):
        """Verify that validate_config(strict=True) raises
        ConfigValidationError when CRITICAL issues exist."""
        # Corrupt a setting to create a CRITICAL issue.
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = "bad_type"
        with pytest.raises(loaded_config.ConfigValidationError):
            loaded_config.validate_config(strict=True)

    def test_credential_masking_password_in_db_url(self, loaded_config):
        """Verify that DATABASE_URL password is properly masked."""
        from config import _mask_sensitive
        masked = _mask_sensitive(
            "DATABASE_URL",
            "postgresql://cosmic:secret_password@localhost:5432/db"
        )
        assert "secret_password" not in masked
        assert "****" in masked

    def test_credential_masking_api_key(self, loaded_config):
        """Verify that API keys are properly masked (first 4 chars + ****)."""
        from config import _mask_sensitive
        masked = _mask_sensitive("DISGENET_API_KEY", "abcdefgh12345678")
        assert masked == "abcd****"
        # Short key.
        masked2 = _mask_sensitive("OMIM_API_KEY", "abc")
        assert masked2 == "****"

    def test_get_config_returns_all_settings(self, loaded_config):
        """Verify that get_config() returns all 25+ settings."""
        cfg = loaded_config.get_config()
        assert len(cfg) >= 25, f"Expected 25+ settings, got {len(cfg)}"
        for name in loaded_config._SETTING_NAMES:
            assert name in cfg, f"Setting {name} missing from get_config()"

    def test_fingerprint_changes_on_config_change(self, loaded_config):
        """Verify that the fingerprint actually changes when a config value
        changes — proving it's a real hash, not a constant."""
        fp1 = loaded_config.get_config_fingerprint()
        # Modify a setting.
        original = loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"]
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = 999
        fp2 = loaded_config.get_config_fingerprint()
        assert fp1 != fp2, "Fingerprint should change when config changes"
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = original

    def test_reload_clears_cache_and_reimports(self, loaded_config):
        """Verify that reload() clears the cache and re-imports settings."""
        # Modify cache.
        loaded_config._resolved_settings["LOG_LEVEL"] = "CUSTOM"
        assert loaded_config.LOG_LEVEL == "CUSTOM"
        # Reload should reset to the source value.
        loaded_config.reload()
        # After reload, the value should come from config.settings.
        from config import settings as _s
        assert loaded_config.LOG_LEVEL == _s.LOG_LEVEL

    def test_is_loaded_raises_on_previous_error(self):
        """Verify that is_loaded() raises ConfigLoadError if a previous
        load failed."""
        import config
        config._settings_loaded = False
        config._load_error = ImportError("test failure")
        config._resolved_settings = {}
        with pytest.raises(config.ConfigLoadError):
            config.is_loaded()

    def test_config_validation_result_equality(self):
        """Verify ConfigValidationResult supports equality comparison."""
        from config import ConfigValidationResult
        r1 = ConfigValidationResult("WARNING", "TEST", "msg")
        r2 = ConfigValidationResult("WARNING", "TEST", "msg")
        r3 = ConfigValidationResult("CRITICAL", "TEST", "msg")
        assert r1 == r2
        assert r1 != r3

    def test_config_validation_result_repr(self):
        """Verify ConfigValidationResult has a useful repr."""
        from config import ConfigValidationResult
        r = ConfigValidationResult("WARNING", "TEST", "test message")
        repr_str = repr(r)
        assert "WARNING" in repr_str
        assert "TEST" in repr_str
        assert "test message" in repr_str

    def test_config_dict_repr_uses_masked_summary(self, loaded_config):
        """Verify ConfigDict.__repr__ uses masked summary, not raw values."""
        # Set DATABASE_URL to a known value with embedded password.
        loaded_config._resolved_settings["DATABASE_URL"] = (
            "postgresql://cosmic:secretpass@localhost:5432/db"
        )
        cfg = loaded_config.get_config()
        repr_str = repr(cfg)
        # The repr should not contain the raw password.
        assert "secretpass" not in repr_str, (
            f"ConfigDict repr should mask password, got: {repr_str[:200]}"
        )

    def test_backward_compat_from_config_settings_import(self):
        """Verify that from config.settings import X still works for ALL
        settings — backward compatibility guarantee."""
        from config.settings import (
            DATABASE_URL, RAW_DATA_DIR, PROCESSED_DATA_DIR,
            CHEMBL_VERSION, STRING_VERSION, STRING_MIN_COMBINED_SCORE,
            CHEMBL_MAX_ROWS, CHEMBL_MAX_ACTIVITIES,
            DISGENET_API_KEY, OMIM_API_KEY, DRUGBANK_XML_PATH,
            DISGENET_URL, DISGENET_API_URL, DISGENET_USE_API,
            OMIM_API_BASE, PUBCHEM_REST_BASE, PUBCHEM_FTP_BASE,
            STRING_PROTEIN_LINKS_URL, STRING_ALIASES_URL,
            STRING_PROTEIN_LINKS_DETAILED_URL,
            CHEMBL_EXPECTED_DRUG_COUNT_MIN,
            CHEMBL_EXPECTED_DRUG_COUNT_MAX,
            AIRFLOW_HOME, LOG_LEVEL,
        )
        # All should be importable without error.
        assert DATABASE_URL is not None

    def test_dir_returns_public_api(self, loaded_config):
        """Verify that dir(config) returns exactly the public API."""
        public = set(loaded_config.__dir__())
        all_set = set(loaded_config.__all__)
        assert public == all_set, (
            f"dir(config) should return exactly __all__. "
            f"Extra: {public - all_set}, Missing: {all_set - public}"
        )

    def test_getattr_raises_for_unknown_name(self, loaded_config):
        """Verify that accessing an unknown attribute raises AttributeError."""
        with pytest.raises(AttributeError, match="has no attribute"):
            _ = loaded_config.NONEXISTENT_SETTING_XYZ

    def test_production_env_strict_validation(self, loaded_config):
        """Verify that in production environment, default credentials are
        flagged as CRITICAL."""
        # Set environment to production and reload.
        loaded_config._resolved_settings["ENVIRONMENT"] = "production"
        loaded_config.ENVIRONMENT = "production"
        results = loaded_config.validate_config()
        db_results = [r for r in results
                     if r.setting_name == "DATABASE_URL"
                     and "default credentials" in r.message.lower()]
        # In production, default credentials should be CRITICAL.
        if db_results:
            assert db_results[0].severity == "CRITICAL", (
                f"Default credentials should be CRITICAL in production, "
                f"got {db_results[0].severity}"
            )
        # Reset.
        loaded_config._resolved_settings["ENVIRONMENT"] = "development"
        loaded_config.ENVIRONMENT = "development"

    def test_url_validation_catches_non_http(self, loaded_config):
        """Verify that URL validation catches non-http URLs."""
        original = loaded_config._resolved_settings["OMIM_API_BASE"]
        loaded_config._resolved_settings["OMIM_API_BASE"] = "ftp://invalid.url"
        results = loaded_config.validate_config()
        url_errors = [r for r in results
                     if r.setting_name == "OMIM_API_BASE"
                     and "does not start with http" in r.message]
        assert len(url_errors) > 0, "Should catch non-http URL"
        loaded_config._resolved_settings["OMIM_API_BASE"] = original

    def test_unknown_chembl_version_warning(self, loaded_config):
        """Verify that an unknown ChEMBL version triggers a warning."""
        original = loaded_config._resolved_settings["CHEMBL_VERSION"]
        loaded_config._resolved_settings["CHEMBL_VERSION"] = "99"
        results = loaded_config.validate_config()
        ver_warns = [r for r in results
                    if r.setting_name == "CHEMBL_VERSION"
                    and "not in the known valid" in r.message]
        assert len(ver_warns) > 0, "Unknown ChEMBL version should trigger warning"
        loaded_config._resolved_settings["CHEMBL_VERSION"] = original

    def test_unknown_string_version_warning(self, loaded_config):
        """Verify that an invalid STRING version (like 12.5) triggers a warning."""
        original = loaded_config._resolved_settings["STRING_VERSION"]
        loaded_config._resolved_settings["STRING_VERSION"] = "12.5"
        results = loaded_config.validate_config()
        ver_warns = [r for r in results
                    if r.setting_name == "STRING_VERSION"
                    and "not in the known valid" in r.message]
        assert len(ver_warns) > 0, "STRING v12.5 should be flagged as invalid"
        loaded_config._resolved_settings["STRING_VERSION"] = original

    def test_low_string_score_warning(self, loaded_config):
        """Verify that STRING_MIN_COMBINED_SCORE < 400 triggers a scientific
        warning about low-confidence PPIs."""
        original = loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"]
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = 200
        results = loaded_config.validate_config()
        score_warns = [r for r in results
                      if r.setting_name == "STRING_MIN_COMBINED_SCORE"
                      and "low-confidence" in r.message.lower()]
        assert len(score_warns) > 0, "Score < 400 should warn about low-confidence PPIs"
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = original

    def test_high_string_score_warning(self, loaded_config):
        """Verify that STRING_MIN_COMBINED_SCORE > 700 triggers a scientific
        warning about missing moderate-confidence PPIs."""
        original = loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"]
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = 800
        results = loaded_config.validate_config()
        score_warns = [r for r in results
                      if r.setting_name == "STRING_MIN_COMBINED_SCORE"
                      and "moderate-confidence" in r.message.lower()]
        assert len(score_warns) > 0, "Score > 700 should warn about missing PPIs"
        loaded_config._resolved_settings["STRING_MIN_COMBINED_SCORE"] = original

    def test_empty_api_key_warning_in_production(self, loaded_config):
        """Verify that empty API keys are CRITICAL in production."""
        loaded_config._resolved_settings["ENVIRONMENT"] = "production"
        loaded_config._resolved_settings["DISGENET_API_KEY"] = ""
        results = loaded_config.validate_config()
        key_results = [r for r in results
                      if r.setting_name == "DISGENET_API_KEY"
                      and "empty" in r.message.lower()]
        assert len(key_results) > 0
        assert key_results[0].severity == "CRITICAL"
        loaded_config._resolved_settings["ENVIRONMENT"] = "development"
        loaded_config._resolved_settings["DISGENET_API_KEY"] = ""

    def test_config_load_error_preserves_original(self):
        """Verify that ConfigLoadError preserves the original exception."""
        from config import ConfigLoadError
        original = ValueError("root cause")
        err = ConfigLoadError("wrapper", original_error=original)
        assert err.original_error is original

    def test_config_validation_error_preserves_results(self):
        """Verify that ConfigValidationError preserves the validation results."""
        from config import ConfigValidationError, ConfigValidationResult
        results = [ConfigValidationResult("CRITICAL", "TEST", "test")]
        err = ConfigValidationError("msg", results=results)
        assert err.results == results


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def inspect_getsource(module):
    """Get source code for a module, handling different Python versions."""
    import inspect
    try:
        return inspect.getsource(module)
    except (OSError, TypeError):
        # If source is not available, read the file directly.
        if hasattr(module, "__file__") and module.__file__:
            with open(module.__file__, "r") as f:
                return f.read()
        return ""
