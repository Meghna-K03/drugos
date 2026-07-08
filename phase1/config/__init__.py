"""
Configuration package for the Drug Repurposing ETL Platform.

This package serves as the single canonical entry point for all configuration
access across the platform. It provides a complete, validated, lazily-loaded
facade over the settings defined in ``config.settings``, with built-in
credential masking, structured validation, and runtime introspection.

Recommended Import Pattern
--------------------------
Prefer ``from config import X`` over ``from config.settings import X``::

    from config import DATABASE_URL
    from config import STRING_MIN_COMBINED_SCORE
    from config import CHEMBL_VERSION, STRING_VERSION

The package-level import is complete — every non-deprecated setting is
available directly.  ``from config.settings import X`` continues to work for
backward compatibility but is not the recommended path.

Lazy Loading
------------
Configuration settings are lazily loaded on first attribute access.  Importing
the ``config`` package does **not** trigger side effects (``load_dotenv``,
``logging.basicConfig``).  Side effects are deferred until the first
configuration attribute is accessed or ``config.initialize()`` is called
explicitly.  This design ensures that test frameworks can import the package
without side effects and control when initialization occurs via
``config.initialize()`` in their ``conftest.py``.

All re-exported settings are resolved dynamically via ``__getattr__`` to
ensure they always reflect the current value in ``config.settings``, even if
that module is later refactored to use descriptors, properties, or computed
attributes.

Sensitive Settings & Credential Masking
---------------------------------------
The following settings are classified as sensitive: ``DATABASE_URL``,
``DISGENET_API_KEY``, ``OMIM_API_KEY``.  Raw values must never be logged.
Use ``get_config_summary()`` to obtain a safe, credential-masked dictionary
suitable for logging and diagnostics.

Security Warning: Never log the output of ``get_config()`` or access raw
credential values in log statements.  Always use ``get_config_summary()``
for any output that may be persisted or displayed.

Configuration Validation
------------------------
``validate_config()`` performs comprehensive checks across all settings:
type correctness, non-emptiness, URL format, numeric ranges, scientific
parameter bounds, and default-credential detection.  In ``strict`` mode,
CRITICAL issues raise ``ConfigValidationError``; otherwise they are returned
as high-severity results alongside warnings.

Environment-specific behavior is controlled by the ``ENVIRONMENT`` setting
(read from the ``ENVIRONMENT`` environment variable, defaulting to
``'development'``).  In production, strict validation is enforced; in
development, warnings are emitted but do not block startup; in test,
missing ``.env`` files are not flagged.

Structured API
--------------
In addition to module-level attribute access, this package provides:

- ``get_config()`` — full configuration as a ``ConfigDict`` dictionary.
- ``get_config_summary()`` — credential-masked summary with provenance.
- ``validate_config(strict=False)`` — validation results list.
- ``initialize(configure_logging=True)`` — explicit eager loading.
- ``reload()`` — re-import settings and clear cache.
- ``is_loaded()`` — check whether settings have been loaded.
- ``get_config_fingerprint()`` — SHA-256 digest for change detection.

Re-export Policy
----------------
All non-deprecated settings from ``config.settings`` are re-exported here.
Deprecated settings (``CHEMBL_URL``, ``UNIPROT_SPROT_URL``,
``UNIPROT_TREMBL_URL``, ``STRING_PROTEIN_INFO_URL``) are **not** re-exported
because they are slated for removal and should not be promoted as part of the
public API.

When adding a new setting to ``config.settings``, it must also be added to
``__all__`` and to the ``_SETTING_NAMES`` list in this file.  The regression
test in ``tests/test_config_init.py`` will catch omissions.

Changelog
---------
v1.0.0 (AUDIT-34) — Initial convenience imports (3 settings).
v2.0.0 — Expanded to full facade with lazy loading, validation, credential
    masking, structured API, and complete re-export of all non-deprecated
    settings across 16 domains.

See Also
--------
config.settings : Full list of settings with their descriptions and defaults.
"""

# ---------------------------------------------------------------------------
# Package version — major when public API changes, minor for features, patch
# for bug-fixes.  v2.0.0 reflects the complete rewrite from 2-line dead-code
# file to comprehensive configuration facade.
# ---------------------------------------------------------------------------
__version__ = "2.0.0"

# ---------------------------------------------------------------------------
# Public API — __all__ defines every name available via ``from config import *``.
# Listed alphabetically; includes all re-exported settings, public functions,
# exception classes, and metadata constants.  The ``settings`` submodule is
# deliberately excluded to prevent leakage of the implementation detail.
# ---------------------------------------------------------------------------
__all__ = (
    # --- Functions ---
    "get_config",
    "get_config_fingerprint",
    "get_config_summary",
    "initialize",
    "is_loaded",
    "reload",
    "validate_config",
    # --- Exceptions ---
    "ConfigLoadError",
    "ConfigValidationError",
    # --- Data classes ---
    "ConfigValidationResult",
    # --- Metadata ---
    "ENVIRONMENT",
    "__version__",
    # --- Settings: Database ---
    "DATABASE_URL",
    # --- Settings: Paths ---
    "AIRFLOW_HOME",
    "BASE_DIR",
    "DRUGBANK_XML_PATH",
    "PROCESSED_DATA_DIR",
    "RAW_DATA_DIR",
    # --- Settings: ChEMBL ---
    "CHEMBL_API_URL",
    "CHEMBL_EXPECTED_DRUG_COUNT_MAX",
    "CHEMBL_EXPECTED_DRUG_COUNT_MIN",
    "CHEMBL_MAX_ACTIVITIES",
    "CHEMBL_MAX_ROWS",
    "CHEMBL_SNAPSHOT_DATE",
    "CHEMBL_VERSION",
    # --- Settings: STRING ---
    "STRING_ALIASES_URL",
    "STRING_MIN_COMBINED_SCORE",
    "STRING_MIN_COMBINED_SCORE_PROD",
    "STRING_PROTEIN_LINKS_DETAILED_URL",
    "STRING_PROTEIN_LINKS_URL",
    "STRING_VERSION",
    "STRING_DETAILED_MODE",
    "STRING_DROP_SELF_INTERACTIONS",
    "STRING_DEDUP_STRATEGY",
    "STRING_LOW_MEMORY",
    "STRING_CHUNK_SIZE",
    # --- Settings: DisGeNET ---
    "DISGENET_API_KEY",
    "DISGENET_API_URL",
    "DISGENET_URL",
    "DISGENET_USE_API",
    # DisGeNET — institutional-grade operational settings (389-fix audit)
    "DISGENET_MIN_SCORE",
    "DISGENET_STRONG_SCORE",  # v41 ROOT FIX (SCIENTIFIC): strong-evidence floor
    "DISGENET_ALLOW_WEAK_EVIDENCE",
    "DISGENET_CONFIDENCE_TIERS_JSON",
    "DISGENET_CONFIDENCE_TIERS",
    "DISGENET_PMID_CAP",
    "DISGENET_PMID_SORT_ORDER",
    "DISGENET_API_PAGE_SIZE",
    "DISGENET_API_MAX_RECORDS",
    "DISGENET_API_TIMEOUT",
    "DISGENET_API_MAX_RETRIES",
    "DISGENET_API_BACKOFF_BASE",
    "DISGENET_API_BACKOFF_MAX_SECONDS",
    "DISGENET_API_MAX_RETRY_AFTER",
    "DISGENET_API_RATE_LIMIT",
    "DISGENET_CIRCUIT_BREAKER_THRESHOLD",
    "DISGENET_CIRCUIT_BREAKER_RESET_SECONDS",
    "DISGENET_CONTACT_EMAIL",
    "DISGENET_ALLOWED_DOMAINS",
    "DISGENET_API_MAX_RESPONSE_BYTES",
    "DISGENET_API_CA_BUNDLE",
    "DISGENET_OUTPUT_FILE_MODE",
    "DISGENET_FALLBACK_TO_CACHE",
    "DISGENET_API_MAX_PAGES",
    "DISGENET_DOWNLOAD_PHASE_TIMEOUT",
    "DISGENET_ALLOW_PARTIAL_DATA",
    "DISGENET_UNIPROT_MAP_TTL_HOURS",
    "DISGENET_TARGET_VERSION",
    "DISGENET_FREEZE_VERSION",
    "DISGENET_MIN_EXPECTED_RECORDS",
    "DISGENET_DISEASE_ONTOLOGY_PATH",
    "DISGENET_HGNC_PATH",
    "DISGENET_MAX_DATA_AGE_DAYS",
    "DISGENET_OUTPUT_FILENAME",
    "DISGENET_RAW_FILENAME",
    "DISGENET_CHUNK_SIZE",
    "DISGENET_API_PARALLEL_PAGES",
    "DISGENET_LOG_FORMAT",
    "DISGENET_ENV",
    "DISGENET_SOURCE_WEIGHTS_JSON",
    "DISGENET_SOURCE_WEIGHTS",
    # --- Settings: OMIM ---
    "OMIM_API_BASE",
    "OMIM_API_KEY",
    # OMIM — institutional-grade operational settings (omim_pipeline.py rewrite)
    "OMIM_REQUEST_INTERVAL",
    "OMIM_MAPPING_KEYS_INCLUDE",
    "OMIM_API_PAGE_LIMIT",
    "OMIM_API_MAX_RETRIES",
    "OMIM_DOWNLOAD_TIMEOUT",
    "OMIM_API_TIMEOUT",
    "OMIM_OUTPUT_FILENAME",
    "OMIM_MIN_EXPECTED_RECORDS",
    "OMIM_MAX_PAGINATION_PAGES",
    "OMIM_DEDUP_KEEP_POLICY",
    "OMIM_CONFIRMED_SCORE",
    "OMIM_CONTIGUOUS_SCORE",
    "OMIM_PHENOTYPE_MAPPED_SCORE",
    "OMIM_GENE_MAPPED_SCORE",
    "OMIM_USER_AGENT",
    "OMIM_API_KEY_FORMAT_RE",
    "OMIM_MAX_AGE_DAYS",
    "OMIM_DB_BATCH_SIZE",
    "OMIM_EXCLUDE_SUSCEPTIBILITY",
    "OMIM_JSON_PRETTY",
    "OMIM_RANDOM_SEED",
    # --- Settings: PubChem ---
    "PUBCHEM_API_URL",
    "PUBCHEM_FTP_BASE",
    "PUBCHEM_REST_BASE",
    # --- Settings: PubChem pipeline (institutional-grade) ---
    "PUBCHEM_PIPELINE_BATCH_SIZE",
    "PUBCHEM_PIPELINE_MIN_BACKOFF",
    "PUBCHEM_PIPELINE_MAX_BACKOFF",
    "PUBCHEM_PIPELINE_READ_TIMEOUT",
    "PUBCHEM_PIPELINE_CACHE_TTL_SECONDS",
    "PUBCHEM_PIPELINE_CONCURRENCY",
    "PUBCHEM_PIPELINE_FETCH_SYNONYMS",
    "PUBCHEM_PIPELINE_FETCH_CAS",
    "PUBCHEM_PIPELINE_SPLIT_RETRY_MAX",
    "PUBCHEM_PIPELINE_MAX_RECORDS",
    "PUBCHEM_PIPELINE_RAW_RESPONSE_RETENTION_DAYS",
    "PUBCHEM_CIRCUIT_BREAKER_THRESHOLD",
    "PUBCHEM_CIRCUIT_BREAKER_RESET_SECONDS",
    "PUBCHEM_PIPELINE_PROPERTIES",
    "PROMETHEUS_ENABLED",
    "OTEL_ENABLED",
    "OPERATOR_ID",
    "RDKIT_AVAILABLE",
    # --- Settings: UniProt ---
    "UNIPROT_RELEASE",
    # --- Settings: Provenance ---
    "DATA_SNAPSHOT_ID",
    # --- Settings: Logging ---
    "LOG_LEVEL",
)

# ---------------------------------------------------------------------------
# Sensitive settings — names whose values must be masked in logs, summaries,
# and any non-debug output.  Not in __all__ because this is an implementation
# detail used by get_config_summary() and validate_config().
#
# v41 ROOT FIX (SEV4): DRUGBANK_XML_PATH was previously listed with comment
# "SEC-6: DrugBank path may leak license info". This is OVERLY CAUTIOUS —
# DRUGBANK_XML_PATH is a filesystem path (e.g.
# "raw_data/drugbank/drugbank_all_full_database.xml.gz"). Masking it adds no
# security: the path is predictable (operators already know where their own
# file is), the DrugBank license terms are about CONTENT redistribution
# (not the path string), and masking the path makes operator debug logs
# (which print the loaded file path) unreadable. Removed from the set.
# ---------------------------------------------------------------------------
SENSITIVE_SETTINGS = frozenset({
    "DATABASE_URL",
    "DISGENET_API_KEY",
    "OMIM_API_KEY",
})

# ---------------------------------------------------------------------------
# Expected types for each re-exported setting.  Used by validate_config() for
# type-checking and by __getattr__ for documentation.  ``None`` in a tuple
# indicates the setting is optional (e.g. CHEMBL_MAX_ROWS can be int or None).
# ---------------------------------------------------------------------------
__annotations__ = {
    "DATABASE_URL": str,
    "AIRFLOW_HOME": "pathlib.Path",
    "BASE_DIR": "pathlib.Path",
    "DRUGBANK_XML_PATH": "pathlib.Path",
    "PROCESSED_DATA_DIR": "pathlib.Path",
    "RAW_DATA_DIR": "pathlib.Path",
    "CHEMBL_API_URL": str,
    "CHEMBL_EXPECTED_DRUG_COUNT_MAX": int,
    "CHEMBL_EXPECTED_DRUG_COUNT_MIN": int,
    "CHEMBL_MAX_ACTIVITIES": (int, type(None)),
    "CHEMBL_MAX_ROWS": (int, type(None)),
    "CHEMBL_SNAPSHOT_DATE": str,
    "CHEMBL_VERSION": str,
    "STRING_ALIASES_URL": str,
    "STRING_MIN_COMBINED_SCORE": int,
    "STRING_MIN_COMBINED_SCORE_PROD": int,
    "STRING_PROTEIN_LINKS_DETAILED_URL": str,
    "STRING_PROTEIN_LINKS_URL": str,
    "STRING_VERSION": str,
    "STRING_DETAILED_MODE": str,
    "STRING_DROP_SELF_INTERACTIONS": bool,
    "STRING_DEDUP_STRATEGY": str,
    "STRING_LOW_MEMORY": bool,
    "STRING_CHUNK_SIZE": int,
    "DISGENET_API_KEY": str,
    "DISGENET_API_URL": str,
    "DISGENET_URL": str,
    "DISGENET_USE_API": bool,
    # DisGeNET — institutional-grade operational settings (389-fix audit)
    "DISGENET_MIN_SCORE": float,
    "DISGENET_STRONG_SCORE": float,  # v41 ROOT FIX (SCIENTIFIC): strong-evidence floor
    "DISGENET_ALLOW_WEAK_EVIDENCE": bool,
    "DISGENET_CONFIDENCE_TIERS_JSON": str,
    "DISGENET_CONFIDENCE_TIERS": list,
    "DISGENET_PMID_CAP": int,
    "DISGENET_PMID_SORT_ORDER": str,
    "DISGENET_API_PAGE_SIZE": int,
    "DISGENET_API_MAX_RECORDS": int,
    "DISGENET_API_TIMEOUT": int,
    "DISGENET_API_MAX_RETRIES": int,
    "DISGENET_API_BACKOFF_BASE": float,
    "DISGENET_API_BACKOFF_MAX_SECONDS": int,
    "DISGENET_API_MAX_RETRY_AFTER": int,
    "DISGENET_API_RATE_LIMIT": float,
    "DISGENET_CIRCUIT_BREAKER_THRESHOLD": int,
    "DISGENET_CIRCUIT_BREAKER_RESET_SECONDS": int,
    "DISGENET_CONTACT_EMAIL": str,
    "DISGENET_ALLOWED_DOMAINS": list,
    "DISGENET_API_MAX_RESPONSE_BYTES": int,
    "DISGENET_API_CA_BUNDLE": str,
    "DISGENET_OUTPUT_FILE_MODE": str,
    "DISGENET_FALLBACK_TO_CACHE": bool,
    "DISGENET_API_MAX_PAGES": int,
    "DISGENET_DOWNLOAD_PHASE_TIMEOUT": int,
    "DISGENET_ALLOW_PARTIAL_DATA": bool,
    "DISGENET_UNIPROT_MAP_TTL_HOURS": int,
    "DISGENET_TARGET_VERSION": str,
    "DISGENET_FREEZE_VERSION": str,
    "DISGENET_MIN_EXPECTED_RECORDS": int,
    "DISGENET_DISEASE_ONTOLOGY_PATH": str,
    "DISGENET_HGNC_PATH": str,
    "DISGENET_MAX_DATA_AGE_DAYS": int,
    "DISGENET_OUTPUT_FILENAME": str,
    "DISGENET_RAW_FILENAME": str,
    "DISGENET_CHUNK_SIZE": int,
    "DISGENET_API_PARALLEL_PAGES": int,
    "DISGENET_LOG_FORMAT": str,
    "DISGENET_ENV": str,
    "DISGENET_SOURCE_WEIGHTS_JSON": str,
    "DISGENET_SOURCE_WEIGHTS": dict,
    "OMIM_API_BASE": str,
    "OMIM_API_KEY": str,
    # OMIM — institutional-grade operational settings
    "OMIM_REQUEST_INTERVAL": float,
    "OMIM_MAPPING_KEYS_INCLUDE": list,
    "OMIM_API_PAGE_LIMIT": int,
    "OMIM_API_MAX_RETRIES": int,
    "OMIM_DOWNLOAD_TIMEOUT": int,
    "OMIM_API_TIMEOUT": int,
    "OMIM_OUTPUT_FILENAME": str,
    "OMIM_MIN_EXPECTED_RECORDS": int,
    "OMIM_MAX_PAGINATION_PAGES": int,
    "OMIM_DEDUP_KEEP_POLICY": str,
    "OMIM_CONFIRMED_SCORE": float,
    "OMIM_CONTIGUOUS_SCORE": float,
    "OMIM_PHENOTYPE_MAPPED_SCORE": float,
    "OMIM_GENE_MAPPED_SCORE": float,
    "OMIM_USER_AGENT": str,
    "OMIM_API_KEY_FORMAT_RE": str,
    "OMIM_MAX_AGE_DAYS": int,
    "OMIM_DB_BATCH_SIZE": int,
    "OMIM_EXCLUDE_SUSCEPTIBILITY": bool,
    "OMIM_JSON_PRETTY": bool,
    "OMIM_RANDOM_SEED": int,
    "PUBCHEM_API_URL": str,
    "PUBCHEM_FTP_BASE": str,
    "PUBCHEM_REST_BASE": str,
    "UNIPROT_RELEASE": str,
    "DATA_SNAPSHOT_ID": str,
    "LOG_LEVEL": str,
    "ENVIRONMENT": str,
    "ORPHAN_GDA_RETENTION_HOURS": int,
    "LOADERS_STRICT_VALIDATION": bool,
    "LOADERS_MAX_RETRY_ATTEMPTS": int,
    "LOADERS_RETRY_BASE_DELAY": float,
    "LOADERS_ENABLE_TIMING": bool,
    "LOADERS_DEAD_LETTER_ENABLED": bool,
    "LOADERS_MAX_DELETE_COUNT": int,
    "BATCH_SIZE_OVERRIDES": dict,
}

# ---------------------------------------------------------------------------
# Canonical list of all non-deprecated setting names that this package
# re-exports.  Used by __getattr__, get_config, validate_config, and the
# regression test.  When a new setting is added to config.settings, add its
# name here AND to __all__ — the test_config_init.py regression test will
# catch omissions.
# ---------------------------------------------------------------------------
_SETTING_NAMES = (
    "DATABASE_URL",
    "AIRFLOW_HOME",
    "BASE_DIR",
    "DRUGBANK_XML_PATH",
    "PROCESSED_DATA_DIR",
    "RAW_DATA_DIR",
    "CHEMBL_API_URL",
    "CHEMBL_EXPECTED_DRUG_COUNT_MAX",
    "CHEMBL_EXPECTED_DRUG_COUNT_MIN",
    "CHEMBL_MAX_ACTIVITIES",
    "CHEMBL_MAX_ROWS",
    "CHEMBL_SNAPSHOT_DATE",
    "CHEMBL_VERSION",
    # ChEMBL — institutional-grade operational settings (chembl_pipeline.py rewrite)
    "CHEMBL_PAGE_SIZE",
    "CHEMBL_MAX_RETRIES",
    "CHEMBL_RETRY_BACKOFF_BASE",
    "CHEMBL_MIN_REQUEST_INTERVAL",
    "CHEMBL_HTTP_TIMEOUT",
    "CHEMBL_MAX_RESPONSE_BYTES",
    "CHEMBL_CIRCUIT_BREAKER_THRESHOLD",
    "CHEMBL_CIRCUIT_BREAKER_RESET_SECONDS",
    "CHEMBL_TARGET_ORGANISM",
    "CHEMBL_MAX_PHASE",
    "CHEMBL_MW_MACROMOLECULE_THRESHOLD",
    "CHEMBL_ACTIVITY_TYPES",
    "CHEMBL_STANDARD_UNITS",
    "CHEMBL_STANDARD_RELATIONS",
    "CHEMBL_ASSAY_TYPES",
    "CHEMBL_TARGET_TYPES",
    "CHEMBL_TARGET_ACCESSION_STRATEGY",
    "CHEMBL_ACTIVITY_CHUNK_SIZE",
    "CHEMBL_DPI_BATCH_SIZE",
    "CHEMBL_TARGET_RESOLUTION_BATCH_SIZE",
    "CHEMBL_API_WORKERS",
    "CHEMBL_TARGET_RESOLUTION_WORKERS",
    "CHEMBL_TARGET_CACHE_TTL_SECONDS",
    "CHEMBL_DRUG_ID_CACHE_TTL_SECONDS",
    "CHEMBL_CACHE_TTL_SECONDS",
    "CHEMBL_ALLOW_VERSION_MISMATCH",
    "CHEMBL_RESUME",
    "CHEMBL_VERSION_COUNT_RANGES",
    "DEFAULT_CHEMBL_VERSION",
    "VALID_CHEMBL_VERSIONS",
    # DrugBank — institutional-grade operational settings (drugbank_pipeline.py rewrite)
    "DRUGBANK_VERSION",
    "DRUGBANK_XML_NAMESPACE",
    "DRUGBANK_TARGET_ORGANISMS",
    "DRUGBANK_GENERATE_SYNTH_KEYS",
    "DRUGBANK_DROP_NO_INCHIKEY",
    "DRUGBANK_CONSERVATIVE_DEFAULTS",
    "DRUGBANK_BATCH_SIZE",
    "DRUGBANK_LOG_INTERVAL",
    "DRUGBANK_MAX_DRUGS",
    "DRUGBANK_EXTRACT_TARGETS",
    "DRUGBANK_EXTRACT_ENZYMES",
    "DRUGBANK_EXTRACT_TRANSPORTERS",
    "DRUGBANK_CSV_COMPRESSION",
    "DRUGBANK_EXPECTED_SHA256",
    "DRUGBANK_EXPECTED_DRUG_COUNT_MIN",
    "DRUGBANK_EXPECTED_DRUG_COUNT_MAX",
    "DRUGBANK_LOG_REDACT",
    "DRUGBANK_LOG_FULL_PATHS",
    "DRUGBANK_VALIDATE_READABILITY",
    "DRUGBANK_DPI_BATCH_SIZE",
    "DEFAULT_DRUGBANK_VERSION",
    "VALID_DRUGBANK_VERSIONS",
    # Pipeline-wide operational settings
    "PIPELINE_RUN_ID",
    "PIPELINE_USE_CACHE",
    "PIPELINE_LOG_FORMAT",
    "PIPELINE_CONTACT_EMAIL",
    "PIPELINE_RESUME",
    "STRING_ALIASES_URL",
    "STRING_MIN_COMBINED_SCORE",
    "STRING_MIN_COMBINED_SCORE_PROD",
    "STRING_PROTEIN_LINKS_DETAILED_URL",
    "STRING_PROTEIN_LINKS_URL",
    "STRING_VERSION",
    "STRING_DETAILED_MODE",
    "STRING_DROP_SELF_INTERACTIONS",
    "STRING_DEDUP_STRATEGY",
    "STRING_LOW_MEMORY",
    "STRING_CHUNK_SIZE",
    "STRING_VERSION_SCORE_THRESHOLDS",
    "DEFAULT_STRING_VERSION",
    "VALID_STRING_VERSIONS",
    "DISGENET_API_KEY",
    "DISGENET_API_URL",
    "DISGENET_URL",
    "DISGENET_USE_API",
    # DisGeNET — institutional-grade operational settings (389-fix audit)
    "DISGENET_MIN_SCORE",
    "DISGENET_STRONG_SCORE",  # v41 ROOT FIX (SCIENTIFIC): strong-evidence floor
    "DISGENET_ALLOW_WEAK_EVIDENCE",
    "DISGENET_CONFIDENCE_TIERS_JSON",
    "DISGENET_CONFIDENCE_TIERS",
    "DISGENET_PMID_CAP",
    "DISGENET_PMID_SORT_ORDER",
    "DISGENET_API_PAGE_SIZE",
    "DISGENET_API_MAX_RECORDS",
    "DISGENET_API_TIMEOUT",
    "DISGENET_API_MAX_RETRIES",
    "DISGENET_API_BACKOFF_BASE",
    "DISGENET_API_BACKOFF_MAX_SECONDS",
    "DISGENET_API_MAX_RETRY_AFTER",
    "DISGENET_API_RATE_LIMIT",
    "DISGENET_CIRCUIT_BREAKER_THRESHOLD",
    "DISGENET_CIRCUIT_BREAKER_RESET_SECONDS",
    "DISGENET_CONTACT_EMAIL",
    "DISGENET_ALLOWED_DOMAINS",
    "DISGENET_API_MAX_RESPONSE_BYTES",
    "DISGENET_API_CA_BUNDLE",
    "DISGENET_OUTPUT_FILE_MODE",
    "DISGENET_FALLBACK_TO_CACHE",
    "DISGENET_API_MAX_PAGES",
    "DISGENET_DOWNLOAD_PHASE_TIMEOUT",
    "DISGENET_ALLOW_PARTIAL_DATA",
    "DISGENET_UNIPROT_MAP_TTL_HOURS",
    "DISGENET_TARGET_VERSION",
    "DISGENET_FREEZE_VERSION",
    "DISGENET_MIN_EXPECTED_RECORDS",
    "DISGENET_DISEASE_ONTOLOGY_PATH",
    "DISGENET_HGNC_PATH",
    "DISGENET_MAX_DATA_AGE_DAYS",
    "DISGENET_OUTPUT_FILENAME",
    "DISGENET_RAW_FILENAME",
    "DISGENET_CHUNK_SIZE",
    "DISGENET_API_PARALLEL_PAGES",
    "DISGENET_LOG_FORMAT",
    "DISGENET_ENV",
    "DISGENET_SOURCE_WEIGHTS_JSON",
    "DISGENET_SOURCE_WEIGHTS",
    "OMIM_API_BASE",
    "OMIM_API_KEY",
    # OMIM — institutional-grade operational settings
    "OMIM_REQUEST_INTERVAL",
    "OMIM_MAPPING_KEYS_INCLUDE",
    "OMIM_API_PAGE_LIMIT",
    "OMIM_API_MAX_RETRIES",
    "OMIM_DOWNLOAD_TIMEOUT",
    "OMIM_API_TIMEOUT",
    "OMIM_OUTPUT_FILENAME",
    "OMIM_MIN_EXPECTED_RECORDS",
    "OMIM_MAX_PAGINATION_PAGES",
    "OMIM_DEDUP_KEEP_POLICY",
    "OMIM_CONFIRMED_SCORE",
    "OMIM_CONTIGUOUS_SCORE",
    "OMIM_PHENOTYPE_MAPPED_SCORE",
    "OMIM_GENE_MAPPED_SCORE",
    "OMIM_USER_AGENT",
    "OMIM_API_KEY_FORMAT_RE",
    "OMIM_MAX_AGE_DAYS",
    "OMIM_DB_BATCH_SIZE",
    "OMIM_EXCLUDE_SUSCEPTIBILITY",
    "OMIM_JSON_PRETTY",
    "OMIM_RANDOM_SEED",
    "PUBCHEM_API_URL",
    "PUBCHEM_FTP_BASE",
    "PUBCHEM_REST_BASE",
    "UNIPROT_RELEASE",
    "DATA_SNAPSHOT_ID",
    "LOG_LEVEL",
    "ENVIRONMENT",
    "ORPHAN_GDA_RETENTION_HOURS",
    "LOADERS_STRICT_VALIDATION",
    "LOADERS_MAX_RETRY_ATTEMPTS",
    "LOADERS_RETRY_BASE_DELAY",
    "LOADERS_ENABLE_TIMING",
    "LOADERS_DEAD_LETTER_ENABLED",
    "LOADERS_MAX_DELETE_COUNT",
    "BATCH_SIZE_OVERRIDES",
    "CONFIG_REGISTRY",
    "ENV_VAR_SCHEMA",
    # Entity Resolution settings (all ENV-overridable)
    "ENTITY_RESOLUTION_PUBCHEM_ENABLED",
    "ENTITY_RESOLUTION_COLLAPSE_STEREOISOMERS",
    "ENTITY_RESOLUTION_FUZZY_THRESHOLD",
    "ENTITY_RESOLUTION_FUZZY_MAX_CANDIDATES",
    "ENTITY_RESOLUTION_PUBCHEM_REST_BASE",
    "ENTITY_RESOLUTION_PUBCHEM_CALL_DELAY",
    "ENTITY_RESOLUTION_PUBCHEM_TIMEOUT",
    "ENTITY_RESOLUTION_PUBCHEM_MAX_RETRIES",
    "ENTITY_RESOLUTION_PUBCHEM_API_KEY",
    "ENTITY_RESOLUTION_PUBCHEM_CA_BUNDLE",
    "ENTITY_RESOLUTION_PUBCHEM_CERT_PEM",
    "ENTITY_RESOLUTION_PUBCHEM_KEY_PEM",
    "ENTITY_RESOLUTION_PUBCHEM_STRICT_SALT_FORM",
    "ENTITY_RESOLUTION_SOURCE_WHITELIST",
    "ENTITY_RESOLUTION_DEFAULT_ORGANISM",
    "ENTITY_RESOLUTION_MAPPING_SCHEMA_VERSION",
    # PubChem pipeline (institutional-grade — fixes PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md).
    "PUBCHEM_PIPELINE_BATCH_SIZE",
    "PUBCHEM_PIPELINE_MIN_BACKOFF",
    "PUBCHEM_PIPELINE_MAX_BACKOFF",
    "PUBCHEM_PIPELINE_READ_TIMEOUT",
    "PUBCHEM_PIPELINE_CACHE_TTL_SECONDS",
    "PUBCHEM_PIPELINE_CONCURRENCY",
    "PUBCHEM_PIPELINE_FETCH_SYNONYMS",
    "PUBCHEM_PIPELINE_FETCH_CAS",
    "PUBCHEM_PIPELINE_SPLIT_RETRY_MAX",
    "PUBCHEM_PIPELINE_MAX_RECORDS",
    "PUBCHEM_PIPELINE_RAW_RESPONSE_RETENTION_DAYS",
    "PUBCHEM_CIRCUIT_BREAKER_THRESHOLD",
    "PUBCHEM_CIRCUIT_BREAKER_RESET_SECONDS",
    "PUBCHEM_PIPELINE_PROPERTIES",
    "PROMETHEUS_ENABLED",
    "OTEL_ENABLED",
    "OPERATOR_ID",
    "RDKIT_AVAILABLE",
)

# Known valid ChEMBL database versions (used by validate_config).
_KNOWN_CHEMBL_VERSIONS = frozenset({"30", "31", "32", "33", "34", "35"})
# Known valid STRING database versions (v11.0b, v11.5, v12.0 — v12.5 does NOT exist).
_KNOWN_STRING_VERSIONS = frozenset({"11.0", "11.0b", "11.5", "12.0"})

# Default DATABASE_URL substring — used to detect default credentials in prod.
_DEFAULT_DB_URL_PREFIX = "postgresql://cosmic:cosmic@"

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfigValidationError(Exception):
    """Raised when configuration validation detects CRITICAL issues in strict mode.

    Attributes
    ----------
    results : list[ConfigValidationResult]
        The full list of validation results that triggered this error.
    """

    def __init__(self, message: str, results: list | None = None):
        super().__init__(message)
        self.results = results or []


class ConfigLoadError(Exception):
    """Raised when the config.settings module cannot be imported.

    This wraps the original ImportError so consumers can distinguish
    between a missing module and a validation failure.
    """

    def __init__(self, message: str, original_error: Exception | None = None):
        super().__init__(message)
        self.original_error = original_error


# ---------------------------------------------------------------------------
# Validation result data class
# ---------------------------------------------------------------------------

class ConfigValidationResult:
    """Structured validation finding for a single configuration check.

    Parameters
    ----------
    severity : str
        One of ``'CRITICAL'``, ``'WARNING'``, ``'INFO'``.
    setting_name : str
        The setting this finding relates to (or ``'__global__'`` for
        cross-setting checks).
    message : str
        Human-readable description of the issue and its impact.
    """

    __slots__ = ("severity", "setting_name", "message")

    def __init__(self, severity: str, setting_name: str, message: str):
        self.severity = severity
        self.setting_name = setting_name
        self.message = message

    def __repr__(self) -> str:
        return (
            f"ConfigValidationResult(severity={self.severity!r}, "
            f"setting_name={self.setting_name!r}, message={self.message!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ConfigValidationResult):
            return NotImplemented
        return (
            self.severity == other.severity
            and self.setting_name == other.setting_name
            and self.message == other.message
        )


# ---------------------------------------------------------------------------
# ConfigDict — lightweight dict subclass for structured access
# ---------------------------------------------------------------------------

class ConfigDict(dict):
    """Dictionary of all current configuration values.

    Returned by ``get_config()``.  Includes a human-readable ``__repr__``
    with masked credentials and a ``to_dict()`` method for JSON serialization.
    """

    def __repr__(self) -> str:
        summary = get_config_summary()
        return f"ConfigDict({summary!r})"

    def to_dict(self) -> dict:
        """Return a plain ``dict`` copy suitable for JSON serialization."""
        return dict(self)


# ---------------------------------------------------------------------------
# Private module-level state
# ---------------------------------------------------------------------------
# _settings_loaded : whether config.settings has been successfully imported
# _load_error      : the ImportError if loading failed, None otherwise
# _resolved_settings : cache of resolved setting values (name → value)
# _configure_logging  : whether logging.basicConfig should be called on load
# ---------------------------------------------------------------------------
_settings_loaded: bool = False
_load_error: Exception | None = None
_resolved_settings: dict = {}
_configure_logging: bool = True


def _ensure_settings_loaded() -> None:
    """Import ``config.settings`` on first access and cache all values.

    This function is the single gateway through which the lazy-loading
    architecture resolves settings.  It is called by ``__getattr__`` on
    the first attribute access, by ``initialize()`` for eager loading, and
    by ``reload()`` to refresh the cache.

    Side effects (``load_dotenv``, ``logging.basicConfig``) are triggered
    by the import of ``config.settings`` itself; this function merely
    controls *when* that import occurs and caches the results.

    Raises
    ------
    ConfigLoadError
        If ``config.settings`` cannot be imported (e.g. missing dependency).
    ConfigValidationError
        If CRITICAL validation issues are detected and the current
        ``ENVIRONMENT`` is ``'production'``.
    """
    global _settings_loaded, _load_error, _resolved_settings

    if _settings_loaded:
        return

    try:
        # Use explicit relative import per PEP 328 for intra-package reference.
        # This ensures the import works correctly even if the package is renamed
        # or nested inside another package.
        from . import settings as _settings_mod

        # Cache every non-deprecated setting by reading its current value
        # dynamically from the settings module.  This ensures the binding is
        # always to the CURRENT value, not a snapshot taken at import time,
        # which matters if settings.py is later refactored to use descriptors
        # or computed attributes.
        _resolved_settings = {}
        for name in _SETTING_NAMES:
            _resolved_settings[name] = getattr(_settings_mod, name)

        # Also cache the ENVIRONMENT constant.
        # Chain 1 root fix: prefer DRUGOS_ENVIRONMENT (canonical) over the
        # legacy ENVIRONMENT variable so docker-compose deploys are honoured.
        _os_mod = __import__("os")
        _resolved_settings["ENVIRONMENT"] = _resolved_settings.get(
            "ENVIRONMENT",
            _os_mod.getenv("DRUGOS_ENVIRONMENT")
            or _os_mod.getenv("ENVIRONMENT", "development"),
        )

        _settings_loaded = True
        _load_error = None

        # Structured logging: record that the config package was loaded,
        # how many settings were resolved, and which source (.env vs defaults)
        # was used.  Avoid logging any raw credential values.
        import logging as _logging
        _logger = _logging.getLogger("config")
        _logger.info(
            "Config package loaded: %d settings resolved (version %s, environment=%s)",
            len(_resolved_settings),
            __version__,
            _resolved_settings.get("ENVIRONMENT", "development"),
        )
        _logger.debug(
            "Re-exported settings: %s",
            ", ".join(sorted(_SETTING_NAMES)),
        )

        # Run validation — in production, CRITICAL issues raise immediately.
        _env = _resolved_settings.get("ENVIRONMENT", "development")
        _strict = _env == "production"
        _results = _run_validation()
        _criticals = [r for r in _results if r.severity == "CRITICAL"]
        if _criticals and _strict:
            raise ConfigValidationError(
                f"{len(_criticals)} CRITICAL config issue(s) in production: "
                + "; ".join(r.message for r in _criticals[:3]),
                results=_results,
            )
        for _r in _results:
            if _r.severity == "WARNING":
                import logging as _l2
                _l2.getLogger("config").warning(
                    "Config warning [%s]: %s", _r.setting_name, _r.message
                )

    except (ConfigValidationError, ConfigLoadError):
        # Re-raise our own exceptions directly.
        raise
    except Exception as exc:
        _load_error = exc
        _settings_loaded = False
        raise ConfigLoadError(
            f"Failed to load config.settings: {exc}",
            original_error=exc,
        ) from exc


def _run_validation() -> list:
    """Execute all validation checks and return a list of ConfigValidationResult.

    This is the internal implementation called by ``_ensure_settings_loaded()``
    and ``validate_config()``.  It does not raise; it collects all findings.
    """
    results: list[ConfigValidationResult] = []

    # If settings haven't been loaded yet, we cannot validate.
    if not _settings_loaded:
        results.append(
            ConfigValidationResult(
                "CRITICAL", "__global__",
                "Cannot validate: config.settings has not been loaded yet."
            )
        )
        return results

    env = _resolved_settings.get("ENVIRONMENT", "development")

    # ---- Type validation ----
    # Verify each setting matches its expected type from __annotations__.
    _type_map = {
        str: (str,),
        int: (int,),
        bool: (bool,),
        "pathlib.Path": ("pathlib.Path",),
    }

    for name in _SETTING_NAMES:
        if name not in _resolved_settings:
            results.append(
                ConfigValidationResult(
                    "CRITICAL", name,
                    f"Setting '{name}' is missing from resolved settings."
                )
            )
            continue

        value = _resolved_settings[name]
        expected = __annotations__.get(name)

        if expected is None:
            continue

        # Normalize expected types for comparison.
        if isinstance(expected, tuple):
            allowed_types = []
            for t in expected:
                if t is type(None):
                    allowed_types.append(type(None))
                elif t is int:
                    allowed_types.append(int)
                elif t is str:
                    allowed_types.append(str)
                elif t is bool:
                    allowed_types.append(bool)
            if value is None and type(None) in allowed_types:
                continue
            if not isinstance(value, tuple(t for t in allowed_types if t is not type(None))):
                # Special case: bool is subclass of int in Python, so check bool first.
                if isinstance(value, bool) and int in allowed_types:
                    # v41 ROOT FIX (SEV3): the comment said "flag it" but no
                    # flag was raised — a bool where an int was expected is
                    # usually a config bug (e.g. DRUGBANK_LOG_REDACT=True where
                    # the schema expects 1/0). Emit a UserWarning so operators
                    # see the type drift in CI logs; do NOT escalate to
                    # CRITICAL because bool is mathematically a subtype of int
                    # (True==1, False==0) so the downstream code WILL work.
                    import warnings as _v41_warnings
                    _v41_warnings.warn(
                        f"Setting {name!r} expected type int but got bool "
                        f"(value={value!r}). This works (bool is a subtype of "
                        f"int) but may indicate a schema/annotation drift — "
                        f"check the setting's __annotation__.",
                        UserWarning,
                        stacklevel=2,
                    )
                else:
                    results.append(
                        ConfigValidationResult(
                            "CRITICAL", name,
                            f"Type mismatch: expected {expected}, got {type(value).__name__} "
                            f"(value={_safe_repr(name, value)})"
                        )
                    )
        elif isinstance(expected, str):
            # String annotation like "pathlib.Path"
            import pathlib
            if expected == "pathlib.Path" and not isinstance(value, pathlib.Path):
                results.append(
                    ConfigValidationResult(
                        "CRITICAL", name,
                        f"Type mismatch: expected pathlib.Path, got {type(value).__name__}"
                    )
                )
        elif expected is str:
            if not isinstance(value, str):
                results.append(
                    ConfigValidationResult(
                        "CRITICAL", name,
                        f"Type mismatch: expected str, got {type(value).__name__}"
                    )
                )
        elif expected is int:
            if not isinstance(value, int) or isinstance(value, bool):
                results.append(
                    ConfigValidationResult(
                        "CRITICAL", name,
                        f"Type mismatch: expected int, got {type(value).__name__}"
                    )
                )
        elif expected is bool:
            if not isinstance(value, bool):
                results.append(
                    ConfigValidationResult(
                        "CRITICAL", name,
                        f"Type mismatch: expected bool, got {type(value).__name__}"
                    )
                )

    # ---- Non-empty validation for string settings ----
    _optional_strings = {"DISGENET_API_KEY", "OMIM_API_KEY"}
    for name in _SETTING_NAMES:
        if name in _optional_strings:
            continue
        value = _resolved_settings.get(name)
        if isinstance(value, str) and not value.strip():
            results.append(
                ConfigValidationResult(
                    "WARNING", name,
                    f"Setting '{name}' is an empty string.  This may indicate a missing "
                    f"environment variable."
                )
            )

    # ---- Path existence validation ----
    import pathlib
    for path_name in ("RAW_DATA_DIR", "PROCESSED_DATA_DIR", "AIRFLOW_HOME"):
        value = _resolved_settings.get(path_name)
        if value is None:
            continue
        if isinstance(value, pathlib.Path):
            # Only warn if the parent doesn't exist (the dir itself may not yet).
            if not value.parent.exists():
                results.append(
                    ConfigValidationResult(
                        "WARNING", path_name,
                        f"Parent directory for {path_name} does not exist: {value.parent}"
                    )
                )

    # ---- URL format validation ----
    _url_settings = (
        "DISGENET_API_URL", "DISGENET_URL", "OMIM_API_BASE",
        "PUBCHEM_FTP_BASE", "PUBCHEM_REST_BASE",
        "STRING_ALIASES_URL", "STRING_PROTEIN_LINKS_DETAILED_URL",
        "STRING_PROTEIN_LINKS_URL",
    )
    for name in _url_settings:
        value = _resolved_settings.get(name)
        if isinstance(value, str) and not value.startswith(("http://", "https://")):
            results.append(
                ConfigValidationResult(
                    "CRITICAL", name,
                    f"URL setting '{name}' does not start with http:// or https://: "
                    f"{_safe_repr(name, value)}"
                )
            )

    # ---- Range validation: STRING_MIN_COMBINED_SCORE (0–1000) ----
    score = _resolved_settings.get("STRING_MIN_COMBINED_SCORE")
    if isinstance(score, int):
        if score < 0 or score > 1000:
            results.append(
                ConfigValidationResult(
                    "CRITICAL", "STRING_MIN_COMBINED_SCORE",
                    f"Value {score} is outside valid range [0, 1000]."
                )
            )
        elif score < 400:
            results.append(
                ConfigValidationResult(
                    "WARNING", "STRING_MIN_COMBINED_SCORE",
                    f"Value {score} is below 400 — includes low-confidence PPIs.  "
                    f"Score >= 400 captures ~5M interactions (25%% of STRING); "
                    f"score >= 700 captures ~500K (2.5%%).  Lower values increase "
                    f"recall but may introduce false positives in drug repurposing."
                )
            )
        elif score > 700:
            results.append(
                ConfigValidationResult(
                    "WARNING", "STRING_MIN_COMBINED_SCORE",
                    f"Value {score} is above 700 — excludes moderate-confidence PPIs.  "
                    f"Score >= 700 captures only ~500K interactions (2.5%% of STRING).  "
                    f"Higher values increase precision but may miss valid interactions."
                )
            )

    # ---- Range validation: CHEMBL_EXPECTED_DRUG_COUNT_MIN < MAX ----
    count_min = _resolved_settings.get("CHEMBL_EXPECTED_DRUG_COUNT_MIN")
    count_max = _resolved_settings.get("CHEMBL_EXPECTED_DRUG_COUNT_MAX")
    if isinstance(count_min, int) and isinstance(count_max, int):
        if count_min >= count_max:
            results.append(
                ConfigValidationResult(
                    "CRITICAL", "CHEMBL_EXPECTED_DRUG_COUNT_MIN",
                    f"CHEMBL_EXPECTED_DRUG_COUNT_MIN ({count_min}) must be less than "
                    f"CHEMBL_EXPECTED_DRUG_COUNT_MAX ({count_max})."
                )
            )
        if count_min <= 0:
            results.append(
                ConfigValidationResult(
                    "WARNING", "CHEMBL_EXPECTED_DRUG_COUNT_MIN",
                    "CHEMBL_EXPECTED_DRUG_COUNT_MIN is 0 or negative — drug count "
                    "validation is effectively disabled."
                )
            )

    # ---- Scientific data version validation ----
    chembl_ver = _resolved_settings.get("CHEMBL_VERSION")
    if isinstance(chembl_ver, str) and chembl_ver not in _KNOWN_CHEMBL_VERSIONS:
        results.append(
            ConfigValidationResult(
                "WARNING", "CHEMBL_VERSION",
                f"CHEMBL_VERSION '{chembl_ver}' is not in the known valid versions "
                f"({_KNOWN_CHEMBL_VERSIONS}).  This may indicate an outdated or "
                f"non-existent database version, which could cause download failures "
                f"or produce different molecule sets than expected."
            )
        )

    string_ver = _resolved_settings.get("STRING_VERSION")
    if isinstance(string_ver, str) and string_ver not in _KNOWN_STRING_VERSIONS:
        results.append(
            ConfigValidationResult(
                "WARNING", "STRING_VERSION",
                f"STRING_VERSION '{string_ver}' is not in the known valid versions "
                f"({_KNOWN_STRING_VERSIONS}).  Note: v12.5 does NOT exist.  Using "
                f"an invalid version will cause download failures."
            )
        )

    # ---- Processing limit warnings ----
    for limit_name in ("CHEMBL_MAX_ROWS", "CHEMBL_MAX_ACTIVITIES"):
        val = _resolved_settings.get(limit_name)
        if val is not None and isinstance(val, int) and val < 100:
            results.append(
                ConfigValidationResult(
                    "WARNING", limit_name,
                    f"{limit_name} is set to {val}, which is very low.  This will "
                    f"silently produce a scientifically incomplete dataset."
                )
            )

    # ---- Default credential detection ----
    db_url = _resolved_settings.get("DATABASE_URL", "")
    if isinstance(db_url, str) and db_url.startswith(_DEFAULT_DB_URL_PREFIX):
        if env == "production":
            results.append(
                ConfigValidationResult(
                    "CRITICAL", "DATABASE_URL",
                    "DATABASE_URL contains default credentials ('cosmic:cosmic').  "
                    "Default credentials must not be used in production."
                )
            )
        else:
            results.append(
                ConfigValidationResult(
                    "WARNING", "DATABASE_URL",
                    "DATABASE_URL contains default credentials ('cosmic:cosmic').  "
                    "Acceptable for development, but must be changed for production."
                )
            )

    # ---- Empty API key warnings ----
    for key_name in ("DISGENET_API_KEY", "OMIM_API_KEY"):
        val = _resolved_settings.get(key_name, "")
        if isinstance(val, str) and not val.strip():
            if env == "production":
                results.append(
                    ConfigValidationResult(
                        "CRITICAL", key_name,
                        f"{key_name} is empty.  The {key_name.split('_')[0]} API "
                        f"requires authentication and will return 403 without a key."
                    )
                )
            else:
                results.append(
                    ConfigValidationResult(
                        "WARNING", key_name,
                        f"{key_name} is empty.  Some API features will be unavailable."
                    )
                )

    # ---- Completeness check: all settings present ----
    for name in _SETTING_NAMES:
        if name not in _resolved_settings:
            results.append(
                ConfigValidationResult(
                    "CRITICAL", name,
                    f"Setting '{name}' is defined in _SETTING_NAMES but not found "
                    f"in config.settings.  This indicates a broken re-export."
                )
            )

    return results


def _safe_repr(name: str, value: object) -> str:
    """Return a safe string representation, masking sensitive values."""
    if name in SENSITIVE_SETTINGS:
        if isinstance(value, str) and len(value) > 4:
            return value[:2] + "****" + value[-2:]
        return "****"
    return repr(value)


def _mask_sensitive(name: str, value: object) -> str:
    """Mask a sensitive value for logging or summary output.

    - DATABASE_URL: mask the password portion only.
    - API keys: show first 4 chars + '****'.
    - Non-sensitive: return str(value).
    """
    if name not in SENSITIVE_SETTINGS:
        return str(value)

    # DATABASE_URL: mask password in the connection string.
    if name == "DATABASE_URL" and isinstance(value, str):
        import re
        return re.sub(
            r"(://[^:]+:)([^@]+)(@)",
            r"\1****\3",
            value,
        )

    # API keys: show first 4 characters only.
    if isinstance(value, str):
        if len(value) <= 4:
            return "****"
        return value[:4] + "****"

    return "****"


# ---------------------------------------------------------------------------
# Environment constant — read once at module load, cached in resolved settings
# on first access.
# ---------------------------------------------------------------------------
import os as _os

# Chain 1 root fix: prefer DRUGOS_ENVIRONMENT (canonical name set by
# docker-compose.yml) and fall back to legacy ENVIRONMENT for backward
# compatibility.
# v41 ROOT FIX (SEV2): normalize the raw env value to the canonical
# {development, staging, production} vocabulary at module level too —
# otherwise operators who set DRUGOS_ENVIRONMENT=dev or ENVIRONMENT=prod
# get the UN-NORMALIZED string here, while config.settings.ENVIRONMENT
# (which DOES normalize) returns the canonical form. This divergence
# means config.ENVIRONMENT != config.settings.ENVIRONMENT for the same
# input env var — a class of bug that silently defeats production-mode
# guards in any code that reads ``from config import ENVIRONMENT``.
# We apply the SAME normalization map as config.settings:2941 (_ENV_NORMALIZATION).
_raw_environment_for_init: str = (
    _os.getenv("DRUGOS_ENVIRONMENT")
    or _os.getenv("ENVIRONMENT", "development")
).lower()
_ENV_NORMALIZATION_FOR_INIT: dict[str, str] = {
    "dev": "development",
    "develop": "development",
    "development": "development",
    "staging": "staging",
    "stage": "staging",
    "prod": "production",
    "production": "production",
}
ENVIRONMENT: str = _ENV_NORMALIZATION_FOR_INIT.get(
    _raw_environment_for_init, _raw_environment_for_init
)


# ---------------------------------------------------------------------------
# Module-level __getattr__ — lazy attribute resolution
# ---------------------------------------------------------------------------
# This function is called by Python when a name is not found in the module's
# namespace.  We use it to lazily load settings on first access, so that
# ``import config`` by itself does NOT trigger side effects (load_dotenv,
# logging.basicConfig).  The first ``config.DATABASE_URL`` access will trigger
# the load.

def __getattr__(name: str):
    """Lazy-load and return a configuration setting by name.

    This implements the lazy-loading architecture: settings are resolved
    dynamically from ``config.settings`` on first access, ensuring they always
    reflect the current value even if that module is refactored later.
    """
    # Handle ENVIRONMENT specially — it may be accessed before full load.
    if name == "ENVIRONMENT":
        _ensure_settings_loaded()
        return _resolved_settings.get("ENVIRONMENT", "development")

    if name in _SETTING_NAMES:
        _ensure_settings_loaded()
        return _resolved_settings[name]

    # Public functions and classes are defined in this module — they should
    # have been found by normal attribute lookup.  If we reach here for a
    # name in __all__, it's a programming error.
    if name in __all__:
        raise AttributeError(
            f"config package has no attribute '{name}' — this name is listed in "
            f"__all__ but is not a setting and has no implementation."
        )

    raise AttributeError(f"module 'config' has no attribute '{name}'")


# ---------------------------------------------------------------------------
# Module-level __dir__ — control dir() output
# ---------------------------------------------------------------------------
# Ensures that ``dir(config)`` returns the public API (names in __all__)
# plus the standard module attributes.  The ``settings`` submodule is
# deliberately excluded because it is an implementation detail.

def __dir__():
    """Return the list of public names for this package."""
    return list(__all__)


# ---------------------------------------------------------------------------
# Public API: initialize
# ---------------------------------------------------------------------------

def initialize(configure_logging: bool = True) -> None:
    """Explicitly trigger eager loading of configuration settings.

    Call this function when you want to control exactly when the side effects
    of importing ``config.settings`` occur (``load_dotenv``,
    ``logging.basicConfig``).  This is particularly useful in test frameworks
    where you want to suppress logging configuration.

    Parameters
    ----------
    configure_logging : bool, optional
        If ``True`` (default), the ``logging.basicConfig()`` call in
        ``config.settings`` will execute normally.  If ``False``, logging
        configuration is suppressed — useful in test environments where the
        test framework manages logging.

    Examples
    --------
    In ``conftest.py``::

        import config
        config.initialize(configure_logging=False)
    """
    global _configure_logging
    _configure_logging = configure_logging
    _ensure_settings_loaded()


# ---------------------------------------------------------------------------
# Public API: reload
# ---------------------------------------------------------------------------

def reload() -> None:
    """Re-import ``config.settings`` and clear the resolved-settings cache.

    Use this when the environment has changed (e.g. after updating ``.env``)
    and you need the configuration to reflect the new values.  This is
    idempotent — calling reload() multiple times is safe.

    Raises
    ------
    ConfigLoadError
        If the re-import fails.
    ConfigValidationError
        If CRITICAL issues are found in production mode.
    """
    global _settings_loaded, _load_error, _resolved_settings
    _settings_loaded = False
    _load_error = None
    _resolved_settings = {}

    # Re-import the settings module to pick up environment changes.
    import importlib
    from . import settings as _settings_mod
    importlib.reload(_settings_mod)

    # Re-read ENVIRONMENT from the environment in case it changed.
    # Chain 1 root fix: prefer DRUGOS_ENVIRONMENT (canonical).
    global ENVIRONMENT
    ENVIRONMENT = (
        _os.getenv("DRUGOS_ENVIRONMENT")
        or _os.getenv("ENVIRONMENT", "development")
    )

    _ensure_settings_loaded()


# ---------------------------------------------------------------------------
# Public API: is_loaded
# ---------------------------------------------------------------------------

def is_loaded() -> bool:
    """Check whether configuration settings have been loaded.

    Returns
    -------
    bool
        ``True`` if settings have been successfully loaded, ``False`` if not
        yet loaded.

    Raises
    ------
    ConfigLoadError
        If a previous load attempt failed.
    """
    if _load_error is not None:
        raise ConfigLoadError(
            f"Previous config load failed: {_load_error}",
            original_error=_load_error,
        )
    return _settings_loaded


# ---------------------------------------------------------------------------
# Public API: validate_config
# ---------------------------------------------------------------------------

def validate_config(strict: bool = False) -> list:
    """Run all configuration validation checks and return the results.

    This function performs comprehensive validation across all re-exported
    settings, checking types, ranges, URL formats, scientific parameters,
    and security concerns.

    Parameters
    ----------
    strict : bool, optional
        If ``True``, raise ``ConfigValidationError`` when any CRITICAL-severity
        issue is found.  If ``False`` (default), return all results without
        raising, allowing the caller to inspect and handle issues.

    Returns
    -------
    list[ConfigValidationResult]
        A list of validation findings, each with ``severity``,
        ``setting_name``, and ``message`` attributes.

    Raises
    ------
    ConfigValidationError
        If ``strict=True`` and any CRITICAL issues are found.

    Examples
    --------
    ::

        results = config.validate_config()
        for r in results:
            print(f"[{r.severity}] {r.setting_name}: {r.message}")

        # In production startup:
        config.validate_config(strict=True)
    """
    _ensure_settings_loaded()
    results = _run_validation()

    if strict:
        criticals = [r for r in results if r.severity == "CRITICAL"]
        if criticals:
            raise ConfigValidationError(
                f"{len(criticals)} CRITICAL config issue(s): "
                + "; ".join(r.message for r in criticals[:5]),
                results=results,
            )

    return results


# ---------------------------------------------------------------------------
# Public API: get_config
# ---------------------------------------------------------------------------

def get_config() -> ConfigDict:
    """Return a ``ConfigDict`` containing all current configuration values.

    The returned dictionary maps setting names to their current values.
    This is suitable for serialization, programmatic inspection, and
    comparison between runs.

    Returns
    -------
    ConfigDict
        Dictionary of all setting name → value pairs.

    Warning
    -------
    The returned dictionary contains RAW credential values.  Do NOT log or
    display this dictionary.  Use ``get_config_summary()`` for safe output.

    Examples
    --------
    ::

        cfg = config.get_config()
        db_url = cfg["DATABASE_URL"]  # raw value — do not log!
    """
    _ensure_settings_loaded()
    return ConfigDict({name: _resolved_settings[name] for name in _SETTING_NAMES
                       if name in _resolved_settings})


# ---------------------------------------------------------------------------
# Public API: get_config_summary
# ---------------------------------------------------------------------------

def get_config_summary() -> dict:
    """Return a credential-masked summary of the current configuration.

    All values for settings listed in ``SENSITIVE_SETTINGS`` are masked
    (passwords hidden, API keys truncated).  The summary also includes
    provenance metadata for traceability.

    Returns
    -------
    dict
        Mapping of setting names to masked string values, plus provenance
        metadata keys prefixed with ``_``.

    Examples
    --------
    ::

        summary = config.get_config_summary()
        logging.info("Current config: %s", summary)
    """
    _ensure_settings_loaded()

    import datetime
    import sys
    import hashlib

    summary = {}
    for name in _SETTING_NAMES:
        if name in _resolved_settings:
            summary[name] = _mask_sensitive(name, _resolved_settings[name])

    # Provenance metadata — included for configuration traceability so that
    # the configuration active during a particular pipeline run can be
    # reconstructed from logs.
    summary["_loaded_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # Check whether a .env file exists next to the project's BASE_DIR.
    # This provenance field helps diagnose missing API key issues.
    try:
        import pathlib as _pathlib
        _base = _resolved_settings.get("BASE_DIR", _os.getcwd())
        if isinstance(_base, _pathlib.Path):
            summary["_env_file_found"] = (_base.parent / ".env").exists()
        else:
            summary["_env_file_found"] = _pathlib.Path(str(_base)).parent.joinpath(".env").exists()
    except Exception:
        summary["_env_file_found"] = False
    summary["_version"] = __version__
    summary["_fingerprint"] = get_config_fingerprint()
    # Chain 1 root fix: prefer DRUGOS_ENVIRONMENT (canonical).
    summary["_environment"] = _resolved_settings.get(
        "ENVIRONMENT",
        _os.getenv("DRUGOS_ENVIRONMENT")
        or _os.getenv("ENVIRONMENT", "development"),
    )
    summary["_python_version"] = sys.version.split()[0]
    summary["_settings_count"] = len(_resolved_settings)

    return summary


# ---------------------------------------------------------------------------
# Public API: get_config_fingerprint
# ---------------------------------------------------------------------------

def get_config_fingerprint() -> str:
    """Return a SHA-256 hex digest of all current configuration values.

    The fingerprint can be compared between runs to detect configuration
    changes.  It deterministically hashes the sorted key-value pairs.

    Returns
    -------
    str
        64-character hex digest string.

    Examples
    --------
    ::

        fp1 = config.get_config_fingerprint()
        # ... after changing .env ...
        config.reload()
        fp2 = config.get_config_fingerprint()
        assert fp1 == fp2, "Configuration changed between runs!"
    """
    _ensure_settings_loaded()

    import hashlib

    parts = []
    for name in sorted(_SETTING_NAMES):
        if name in _resolved_settings:
            # Mask sensitive values in the fingerprint too — the fingerprint
            # should detect value changes without embedding raw credentials.
            val = _mask_sensitive(name, _resolved_settings[name])
            parts.append(f"{name}={val}")

    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest
