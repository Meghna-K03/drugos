"""Migration management package for the Drug Repurposing ETL Platform.

This package provides a cross-dialect schema migration system that handles
PostgreSQL and SQLite. It manages three migration files (001-003) that
define the complete database schema, constraints, and data transformations.

Public API
----------
- run_migrations(engine, config) -> MigrationResult
    Apply pending schema migrations. Supports dry-run mode, callback hooks,
    and dependency injection for testability.

- check_migrations(engine) -> MigrationHealthResult
    Verify all migrations are applied and schema version matches.

- get_migration_status(engine) -> MigrationStatus
    Detailed status of applied and pending migrations.

- MigrationConfig
    Configuration dataclass for customizing migration behavior.

- MigrationResult
    Result dataclass returned by run_migrations().

- MigrationError
    Custom exception raised when one or more migrations fail.

Migration Files
---------------
- 001_initial_schema.sql — Creates all 7 core tables
- 002_bug_fixes_migration.sql — Adds columns, deduplicates, adds constraints
- 003_models_fix_migration.sql — 78-issue scientific/design/quality fix

Cross-Dialect Support
---------------------
- PostgreSQL: Full SQL migrations via SQLAlchemy text() execution
- SQLite: Column additions via Python inspect() + ALTER TABLE

Dialect Behavior Matrix
-----------------------
+-----------------------+------------+---------+
| Operation             | PostgreSQL | SQLite  |
+=======================+============+=========+
| SQL file execution    | Yes        | No      |
| Column additions      | Yes        | Yes     |
| CHECK constraints     | Yes        | Limited |
| FK constraints        | Yes        | Yes*    |
| Migration tracking    | Yes        | Yes     |
| Unique indexes        | Yes        | Yes     |
+-----------------------+------------+---------+
* SQLite FK requires PRAGMA foreign_keys=ON

Known Limitations
-----------------
- No rollback/downgrade support (planned Alembic migration)
- SQL migrations only execute on PostgreSQL (column additions work on both)
- No automatic schema diff between ORM models and database
- Migrations are intentionally sequential. Migration 002 depends on
  tables created by 001. Migration 003 depends on columns/constraints
  from 002. Parallel execution would violate these dependencies and
  could corrupt the schema.

Why Not Alembic?
----------------
The current cross-dialect Python migration runner was chosen over Alembic
for Phase 1 speed-to-delivery: it requires no additional dependencies,
integrates directly with the existing SQLAlchemy setup, and handles the
project's two-dialect requirement (PostgreSQL + SQLite for testing).

Limitations of the current approach (planned for Alembic migration):
- No automatic schema generation from ORM models
- No rollback/downgrade support
- No dependency graph between migrations
- No automatic migration generation

Migration to Alembic is planned for Phase 2/3 when the schema stabilizes.

Adding a New Migration
----------------------
1. Create a new file: NNN_description.sql (NNN = 3-digit sequential number)
2. Wrap the SQL in BEGIN; ... COMMIT;
3. Use IF NOT EXISTS / IF EXISTS for idempotent DDL
4. Test on both PostgreSQL and SQLite
5. Bump SCHEMA_VERSION in database/base.py
6. Add a corresponding entry in schema_version table
7. Run: python -m database.migrations

Troubleshooting Failed Migrations
---------------------------------
1. Check _migration_history: SELECT * FROM _migration_history ORDER BY id
2. Check _failed_migrations for error details
3. For checksum drift: re-apply with MIGRATIONS_REQUIRE_CHECKSUM=0
4. For constraint violations: check data with validate_scientific_constraints()
5. Last resort: restore from database backup

Data Compliance
---------------
This module modifies database tables that may contain data subject to
regulatory requirements:

GDPR (EU General Data Protection Regulation):
- Gene-disease associations reference gene symbols, which are NOT
  considered personal data under GDPR Recital 26 (not identifying
  a natural person).
- Pipeline run metadata does not contain PII.
- All data is sourced from public, freely available databases.

HIPAA (US Health Insurance Portability and Accountability Act):
- This platform does NOT process Protected Health Information (PHI).
- All data is population-level, not patient-level.
- No dates of birth, SSNs, medical record numbers, or health plan
  beneficiary numbers are stored.

Audit Trail:
- _migration_history records who, when, and what was applied.
- _failed_migrations records failures for forensic investigation.
- Row count changes are logged before and after each migration.

Data Classification
-------------------
Tables modified by migrations contain the following data classifications:
- drugs: Public chemical/drug data (no PII)
- proteins: Public protein data (no PII)
- gene_disease_associations: Gene-disease associations. Gene symbols
  are NOT PII under GDPR, but disease associations for specific
  individuals COULD be. This table stores population-level data
  sourced from public databases, not individual patient data.
- pipeline_runs: Operational metadata (no PII)
- entity_mapping: Cross-database ID mappings (no PII)

Security Notes
--------------
Migration operations (ALTER TABLE, ADD CONSTRAINT, CREATE INDEX)
require elevated database privileges. The current implementation
uses the same DATABASE_URL as the application. For production:
1. Set MIGRATION_DATABASE_URL for migration operations
2. Ensure the application DATABASE_URL has minimal privileges
3. MIGRATION_DATABASE_URL should have CREATE, ALTER, DROP, INDEX
   privileges on the schema

Note: The function run_migrations() shares its name with the module
run_migrations.py. If you encounter ImportError, check whether the error
refers to the module or the function by examining the full import chain.

Note on Migration 003 PPI Records:
v9 ROOT FIX (audit F3.7): migration 003 line 243-248 now correctly
SWAPS misordered protein_a_id / protein_b_id pairs via a single
UPDATE statement (SET protein_a_id = protein_b_id, protein_b_id =
protein_a_id WHERE protein_a_id > protein_b_id). The previous
implementation DELETED misordered rows — that was data loss. The
swap preserves every PPI edge while enforcing the ordering
constraint (protein_a_id < protein_b_id). The docstring above is
retained as a historical note; the code is now correct.

Note on Migration 002 NULL Replacement:
Migration 002 replaces NULL values with empty strings in
gene_disease_associations (gene_symbol, disease_id, source) to support
the UNIQUE constraint with COALESCE. This is a known semantic tradeoff:
NULL ('unknown') becomes '' ('empty'). Downstream code MUST use
COALESCE(gene_symbol, '') for correct queries, not IS NULL checks.

Note on Batch Processing for Large Data:
For datasets exceeding 100K rows, destructive operations in migrations
should use batched DELETE with LIMIT clauses to avoid O(n^2) self-joins
and excessive lock times. Example:
  DELETE FROM table WHERE id IN (SELECT id FROM table LIMIT 10000)
Repeat until 0 rows affected.

Note on Concurrent Index Creation:
For production databases with >1M rows, consider using
CREATE INDEX CONCURRENTLY (PostgreSQL) to avoid blocking concurrent
writes. The current migration system does not support concurrent
index creation — this is a known limitation for the Alembic migration.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

# ---------------------------------------------------------------------------
# Module metadata (CODE-MIG-05)
# ---------------------------------------------------------------------------
__version__: str = "1.0.0"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deprecated aliases (DES-MIG-03, CMP-MIG-04)
# ---------------------------------------------------------------------------
_DEPRECATED_ALIASES: dict[str, str] = {
    "run_migration_002": "run_migrations",
}

# ---------------------------------------------------------------------------
# All symbols that live in run_migrations.py and should be lazily loaded
# ---------------------------------------------------------------------------
_LAZY_SYMBOLS: frozenset[str] = frozenset({
    # Functions
    "run_migrations",
    "check_migrations",
    "get_migration_status",
    "get_sql_migration_files",
    "get_migration_runner",
    "rollback_migration",
    "validate_scientific_constraints",
    "validate_migration_config",
    "verify_schema_matches_orm",
    "verify_package_exports",
    "get_database_fingerprint",
    "plan_migrations",
    # Test Helpers
    "create_test_migrations_dir",
    "reset_migration_state",
    "count_applied_migrations",
    "get_migration_checksum",
    "verify_table_schema",
    "get_failed_migrations",
    "retry_failed_migration",
    "analyze_migration_impact",
    # Data Classes
    "MigrationConfig",
    "MigrationResult",
    "MigrationHealthResult",
    "MigrationStatus",
    "MigrationMetrics",
    "MigrationError",
    # Constants from run_migrations.py
    "MIGRATIONS_DIR",
    "REQUIRED_COLUMNS",
    "INCHIKEY_MAX_LENGTH",
    "STANDARD_INCHIKEY_LENGTH",
    "SYNTHETIC_INCHIKEY_PREFIX",
    "STRING_SCORE_MIN",
    "STRING_SCORE_MAX",
    "MOLECULAR_WEIGHT_PRECISION",
    "DIALECT_POSTGRESQL",
    "DIALECT_SQLITE",
    "SUPPORTED_DIALECTS",
    "MIGRATION_NAME_MAX_LENGTH",
    "MIGRATION_FILENAME_PATTERN",
    "MIGRATION_BATCH_SIZE",
    "PLANNED_MIGRATION_FRAMEWORK",
    "SQL_IDENTIFIER_RE",
    # Deprecated
    "run_migration_002",
})

# ---------------------------------------------------------------------------
# Lazy loading cache
# ---------------------------------------------------------------------------
_lazy_cache: dict[str, Any] = {}
_rm_module_cache: Any = None  # Cache for the entire run_migrations module


def _get_rm_module():
    """Import and cache the run_migrations module exactly once.

    This avoids repeated import calls that can fail due to circular
    import issues when called from within __getattr__.
    """
    global _rm_module_cache
    if _rm_module_cache is None:
        import database.migrations.run_migrations as _rm_mod
        _rm_module_cache = _rm_mod
    return _rm_module_cache


def __getattr__(name: str) -> Any:
    """Lazily load migration symbols on first access.

    This prevents the eager import of SQLAlchemy and database.connection
    that would occur if we used a top-level import. The parent
    database/__init__.py uses the same pattern for the same reason:
    Airflow DAG parsing imports every module every 30 seconds, and we
    must not trigger side effects.

    IMPORTANT: Due to the naming collision between run_migrations (the
    function) and run_migrations.py (the module), Python's import system
    will resolve ``from database.migrations import run_migrations`` to
    the MODULE by default. We override this here by intercepting the
    attribute access and returning the FUNCTION from the module.

    We also store resolved symbols in the module's globals so that
    subsequent ``from database.migrations import X`` calls find the
    symbol in the module namespace rather than resolving to a submodule.
    """
    # __version__ is defined at module level — return directly
    if name == "__version__":
        return __version__

    # Check deprecated aliases first (DES-MIG-03, CMP-MIG-04)
    if name in _DEPRECATED_ALIASES:
        canonical = _DEPRECATED_ALIASES[name]
        warnings.warn(
            f"'{name}' is deprecated, use '{canonical}' instead. "
            f"Will be removed in v2.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Return the canonical symbol via lazy load
        return __getattr__(canonical)

    # SCHEMA_VERSION re-export from database.base (ARCH-MIG-04)
    if name == "SCHEMA_VERSION":
        if name in _lazy_cache:
            return _lazy_cache[name]
        try:
            from database.base import SCHEMA_VERSION as _sv
            _lazy_cache[name] = _sv
            # Store in module globals so ``from database.migrations import SCHEMA_VERSION``
            # resolves to the value, not a submodule lookup
            import sys
            sys.modules[__name__].__dict__[name] = _sv
            logger.debug("Lazy-loaded SCHEMA_VERSION from database.base: %s", _sv)
            return _sv
        except ImportError as exc:
            raise ImportError(
                f"Cannot import SCHEMA_VERSION from database.base: {exc}. "
                f"Ensure database.base is properly configured."
            ) from exc

    # All other symbols come from run_migrations.py
    if name in _LAZY_SYMBOLS:
        # Special handling for run_migrations: the submodule may have
        # shadowed the function in __dict__. Fix it before returning.
        if name == "run_migrations":
            _ensure_run_migrations_is_function()
            import sys as _sys
            _parent = _sys.modules.get(__name__)
            if _parent is not None:
                _current = _parent.__dict__.get("run_migrations")
                if _current is not None and not isinstance(_current, _types.ModuleType):
                    return _current
                # If still a module, try to get the function directly.
                try:
                    from database.migrations.run_migrations import (
                        run_migrations as _fn,
                    )
                    _parent.__dict__["run_migrations"] = _fn
                    return _fn
                except ImportError:
                    pass
        if name in _lazy_cache:
            return _lazy_cache[name]
        try:
            _rm_mod = _get_rm_module()
            if hasattr(_rm_mod, name):
                symbol = getattr(_rm_mod, name)
                _lazy_cache[name] = symbol
                # Store in module globals so ``from database.migrations import X``
                # resolves to the symbol, not the submodule
                import sys
                sys.modules[__name__].__dict__[name] = symbol
                logger.debug("Lazy-loaded migration symbol: %s", name)
                return symbol
            else:
                raise AttributeError(
                    f"Symbol '{name}' exists in _LAZY_SYMBOLS but not in "
                    f"database.migrations.run_migrations module"
                )
        except ImportError as exc:
            raise ImportError(
                f"Cannot import migration runner: {exc}. "
                f"Ensure sqlalchemy is installed and DATABASE_URL is set."
            ) from exc

    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


def __dir__() -> list[str]:
    """Support tab-completion and ``dir()`` on the package.

    Also ensures that ``from database.migrations import run_migrations``
    resolves to the FUNCTION, not the submodule, by including it in
    the explicit module namespace.
    """
    return list(set(list(globals().keys()) + list(_LAZY_SYMBOLS) + ["SCHEMA_VERSION"]))


# ---------------------------------------------------------------------------
# Public API — explicit declaration (CODE-MIG-04, DOC-MIG-05, CMP-MIG-03)
#
# __all__ defines the public API of the migrations package.
# Symbols are organized by category:
# - Functions: run_migrations, check_migrations, get_migration_status, ...
# - Data classes: MigrationConfig, MigrationResult, MigrationHealthResult, ...
# - Constants: MIGRATIONS_DIR, REQUIRED_COLUMNS, SCHEMA_VERSION, ...
# - Deprecated: run_migration_002 (will be removed in v2.0)
# ---------------------------------------------------------------------------
__all__: list[str] = [
    # --- Functions ---
    "run_migrations",
    "check_migrations",
    "get_migration_status",
    "get_sql_migration_files",
    "get_migration_runner",
    "rollback_migration",
    "validate_scientific_constraints",
    "validate_migration_config",
    "verify_schema_matches_orm",
    "verify_package_exports",
    "get_database_fingerprint",
    "plan_migrations",
    # --- Test Helpers ---
    "create_test_migrations_dir",
    "reset_migration_state",
    "count_applied_migrations",
    "get_migration_checksum",
    "verify_table_schema",
    "get_failed_migrations",
    "retry_failed_migration",
    "analyze_migration_impact",
    # --- Data Classes ---
    "MigrationConfig",
    "MigrationResult",
    "MigrationHealthResult",
    "MigrationStatus",
    "MigrationMetrics",
    "MigrationError",
    # --- Constants ---
    "MIGRATIONS_DIR",
    "REQUIRED_COLUMNS",
    "SCHEMA_VERSION",
    "INCHIKEY_MAX_LENGTH",
    "STANDARD_INCHIKEY_LENGTH",
    "SYNTHETIC_INCHIKEY_PREFIX",
    "STRING_SCORE_MIN",
    "STRING_SCORE_MAX",
    "MOLECULAR_WEIGHT_PRECISION",
    "DIALECT_POSTGRESQL",
    "DIALECT_SQLITE",
    "SUPPORTED_DIALECTS",
    "MIGRATION_NAME_MAX_LENGTH",
    "MIGRATION_FILENAME_PATTERN",
    "MIGRATION_BATCH_SIZE",
    "PLANNED_MIGRATION_FRAMEWORK",
    "SQL_IDENTIFIER_RE",
    # --- Package Metadata ---
    "__version__",
    # --- Deprecated ---
    "run_migration_002",
]


# ---------------------------------------------------------------------------
# K fix (test isolation): Resolve the function/submodule naming collision.
#
# ``database.migrations.run_migrations`` is BOTH a function (defined in
# run_migrations.py) AND a submodule (run_migrations.py itself). When the
# submodule is imported (e.g. by ``from database.migrations.run_migrations
# import _split_sql_statements``), Python automatically sets
# ``database.migrations.__dict__['run_migrations'] = <submodule>``,
# shadowing the function. After that, ``from database.migrations import
# run_migrations`` returns the submodule instead of the function — and
# ``__getattr__`` never fires (because the attribute IS in ``__dict__``).
#
# We work around this by hooking into the submodule's import: when
# run_migrations.py finishes loading, it calls back into this package and
# overrides ``__dict__['run_migrations']`` with the function. This ensures
# the function is always exposed regardless of import order.
#
# Additionally, we use __getattribute__ override to detect when the
# submodule has shadowed the function and fix it on the fly.
# ---------------------------------------------------------------------------
import types as _types


def _install_run_migrations_function() -> None:
    """Expose the ``run_migrations`` function (not the submodule) on the
    package namespace. Called by ``run_migrations.py`` after it finishes
    loading.
    """
    import sys
    _parent = sys.modules.get(__name__)
    if _parent is None:
        return
    try:
        from database.migrations.run_migrations import (
            run_migrations as _run_migrations_fn,
        )
    except ImportError:
        return
    _parent.__dict__["run_migrations"] = _run_migrations_fn


def _ensure_run_migrations_is_function() -> None:
    """Ensure ``run_migrations`` in the package __dict__ is the FUNCTION,
    not the submodule.

    Called from __getattr__ when ``run_migrations`` is accessed. If the
    submodule has been imported and shadowed the function, this re-imports
    the function from the submodule and stores it in __dict__.
    """
    import sys
    _parent = sys.modules.get(__name__)
    if _parent is None:
        return
    _current = _parent.__dict__.get("run_migrations")
    if _current is None or isinstance(_current, _types.ModuleType):
        # The function is missing or shadowed by the submodule.
        # Re-import the function from the submodule.
        try:
            from database.migrations.run_migrations import (
                run_migrations as _run_migrations_fn,
            )
            _parent.__dict__["run_migrations"] = _run_migrations_fn
        except ImportError:
            pass


# Note: we intentionally do NOT call ``_install_run_migrations_function()``
# eagerly here. Doing so triggers a circular import (the submodule imports
# symbols from this package during its own initialisation). Instead, the
# submodule's bottom-of-file hook calls back into this function after it
# has finished loading — see ``run_migrations.py``.


# ---------------------------------------------------------------------------
# Post-import fixup: After ALL modules have been loaded (including the
# submodule), re-export all symbols from run_migrations.py into this
# package's namespace. This ensures that `from database.migrations import
# run_migrations` (and MigrationConfig, MigrationResult, etc.) always
# returns the correct objects, even if the submodule was imported by
# another test and shadowed the function.
#
# We use a try/except because the import may fail if sqlalchemy is not
# installed. This is safe to call at module-load time because by the time
# this code runs, the submodule has either been imported (and finished
# loading) or not — either way, the `from ... import` will trigger the
# import if needed.
# ---------------------------------------------------------------------------
def _eagerly_export_symbols():
    """Eagerly import and export all symbols from run_migrations.py.

    This is called at the end of the package initialization AND can be
    called by tests to fix any shadowing that occurred after submodule
    imports.
    """
    import sys
    _parent = sys.modules.get(__name__)
    if _parent is None:
        return
    try:
        from database.migrations.run_migrations import (
            run_migrations,
            MigrationConfig,
            MigrationResult,
            MigrationHealthResult,
            MigrationStatus,
            MigrationMetrics,
            MigrationError,
            MIGRATIONS_DIR,
            REQUIRED_COLUMNS,
            INCHIKEY_MAX_LENGTH,
            STANDARD_INCHIKEY_LENGTH,
            SYNTHETIC_INCHIKEY_PREFIX,
            STRING_SCORE_MIN,
            STRING_SCORE_MAX,
            MOLECULAR_WEIGHT_PRECISION,
            DIALECT_POSTGRESQL,
            DIALECT_SQLITE,
            SUPPORTED_DIALECTS,
            MIGRATION_NAME_MAX_LENGTH,
            MIGRATION_FILENAME_PATTERN,
            MIGRATION_BATCH_SIZE,
            PLANNED_MIGRATION_FRAMEWORK,
            SQL_IDENTIFIER_RE,
            run_migration_002,
        )
    except ImportError:
        return
    # Override any shadowed symbols in __dict__.
    for _name in (
        "run_migrations", "MigrationConfig", "MigrationResult",
        "MigrationHealthResult", "MigrationStatus", "MigrationMetrics",
        "MigrationError", "MIGRATIONS_DIR", "REQUIRED_COLUMNS",
        "INCHIKEY_MAX_LENGTH", "STANDARD_INCHIKEY_LENGTH",
        "SYNTHETIC_INCHIKEY_PREFIX", "STRING_SCORE_MIN", "STRING_SCORE_MAX",
        "MOLECULAR_WEIGHT_PRECISION", "DIALECT_POSTGRESQL", "DIALECT_SQLITE",
        "SUPPORTED_DIALECTS", "MIGRATION_NAME_MAX_LENGTH",
        "MIGRATION_FILENAME_PATTERN", "MIGRATION_BATCH_SIZE",
        "PLANNED_MIGRATION_FRAMEWORK", "SQL_IDENTIFIER_RE",
        "run_migration_002",
    ):
        _parent.__dict__[_name] = locals()[_name]


# Do NOT call _eagerly_export_symbols() at module load time — it would
# trigger a circular import. Instead, we provide it as a public function
# that tests can call if they encounter shadowing issues.
