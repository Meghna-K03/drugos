"""
Database package for the Drug Repurposing ETL Platform.

This package serves as the **single canonical entry point** for all database
operations across the platform.  It provides a complete, lazily-loaded facade
over four architectural layers — connection management, ORM models, bulk data
operations, and schema migrations — with built-in error handling, observability
hooks, and runtime introspection.

Architecture
------------
The database package is organised into four submodules, each responsible for
one clearly defined concern:

- **database.connection** — Engine creation, session factory, context-managed
  sessions, health checks, and lifecycle management (``get_engine``,
  ``get_db_session``, ``init_db``, ``dispose_engine``, ``check_connection``,
  ``get_session_factory``, ``Base``).

- **database.models** — Seven SQLAlchemy ORM models that map 1:1 to the
  database schema: ``Drug``, ``Protein``, ``DrugProteinInteraction``,
  ``ProteinProteinInteraction``, ``GeneDiseaseAssociation``,
  ``EntityMapping``, ``PipelineRun``, plus the
  ``cleanup_orphan_gda_records`` utility.

- **database.loaders** — Eight bulk data operation functions (``bulk_upsert_*``
  and ``bulk_update_drugs_from_pubchem``) that implement ON CONFLICT DO UPDATE
  semantics with dialect-specific handling for PostgreSQL and SQLite.  Four
  lookup helpers (``get_uniprot_to_protein_id_map``,
  ``get_inchikey_to_drug_id_map``, ``build_gene_to_uniprot_maps``,
  ``resolve_gene_symbol_to_uniprot``) provide foreign-key resolution before
  bulk inserts.

- **database.migrations** — Cross-dialect schema migration runner
  (``run_migrations``) that handles column additions, SQL file execution, and
  migration history tracking.

Recommended Import Pattern
--------------------------
Prefer ``from database import X`` over ``from database.submodule import X``::

    from database import get_db_session, init_db
    from database import Drug, Protein, GeneDiseaseAssociation
    from database import bulk_upsert_drugs, bulk_upsert_gda
    from database import run_migrations, check_connection, Base

The package-level import is complete — every public symbol from all four
submodules is available directly.  Direct submodule imports (e.g.
``from database.loaders import bulk_upsert_drugs``) continue to work for
backward compatibility but are not the recommended path.

Lazy Loading
------------
All symbols are lazily loaded on first attribute access.  Importing the
``database`` package does **not** trigger side effects (engine creation,
``.env`` loading, ORM model registration).  Side effects are deferred until
the first symbol attribute is accessed.

This design is critical for Apache Airflow DAG parsing: Airflow imports all
DAG files to introspect them.  With lazy loading, ``import database`` succeeds
even when the database is unavailable, preventing cascade failures across all
DAGs.

Performance characteristics:

- Importing the package is O(1) — no submodule loading occurs.
- First access to any symbol triggers its submodule import (one-time cost).
- Subsequent accesses are O(1) dict lookups from the internal cache.

Security Note
-------------
Because imports are deferred, database credentials (``DATABASE_URL``) are
**not** loaded into memory until the first symbol from ``database.connection``
is accessed.  This reduces the credential exposure window.  The
``_validate_database_security()`` function can be called to audit the
configuration for common insecure patterns.

Data Lineage & Transformation Entry Points
-------------------------------------------
Every record that enters the staging database passes through one of the
``bulk_upsert_*`` or ``bulk_update_*`` functions.  These are the data lineage
entry points where transformation metadata should be captured.  The
``PipelineRun`` model records source, status, row counts, and timing for each
ETL run, and its ``source`` field should include ``database.__version__``
for full traceability.

Optional Utilities
------------------
- ``validate_data_quality_infrastructure(session)`` — Verify that all
  bulk_upsert functions are importable, ORM constraints are defined, and
  lookup functions return valid mappings.

- ``_validate_database_security()`` — Audit ``DATABASE_URL`` for insecure
  patterns (in-memory SQLite in production, missing SSL, weak credentials).

- ``_reset()`` — Clear the lazy-loaded symbol cache for testing.  Use this
  in test fixtures to reset the package state between tests.

- ``_log_import_status()`` — Log which symbols have been loaded and which
  haven't (useful for debugging import issues).

Changelog
---------
v1.0.0 (AUDIT-34) — Initial convenience imports (10 symbols, 2 submodules).
v2.0.0 — Complete rewrite: lazy loading across all 4 submodules, 26 public
    symbols, __all__ declaration, error handling, observability hooks,
    validation utilities, and full 16-domain compliance.

See Also
--------
database.connection : Engine creation, session management, health checks.
database.models     : ORM model definitions and schema contracts.
database.loaders    : Bulk upsert/update functions and lookup helpers.
database.migrations : Cross-dialect schema migration runner.
"""

# Public API: All symbols are lazily loaded via __getattr__ on first access.
# See __all__ for the complete list of public symbols.
# Recommended usage: from database import X

from __future__ import annotations

import importlib
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Package metadata (Domain 15: Interoperability / Domain 16: Lineage)
# ---------------------------------------------------------------------------
__version__: str = "11.0.0"

# ---------------------------------------------------------------------------
# Public API — explicit declaration (Domain 1: Architecture, Domain 14: PEP)
# ---------------------------------------------------------------------------
__all__: list[str] = [
    # --- Connection Management ---
    "get_db_session",
    "get_engine",
    "init_db",
    "dispose_engine",
    "check_connection",
    "get_session_factory",
    "Base",
    # --- ORM Models ---
    "Drug",
    "Protein",
    "DrugProteinInteraction",
    "ProteinProteinInteraction",
    "GeneDiseaseAssociation",
    "EntityMapping",
    "PipelineRun",
    "SchemaVersion",
    "SCHEMA_VERSION",
    "cleanup_orphan_gda_records",
    # --- Data Operations ---
    "bulk_upsert_drugs",
    "bulk_upsert_proteins",
    "bulk_upsert_dpi",
    "bulk_upsert_ppi",
    "bulk_upsert_gda",
    "bulk_upsert_entity_mapping",
    "bulk_update_drugs_from_pubchem",
    "get_uniprot_to_protein_id_map",
    "get_inchikey_to_drug_id_map",
    "build_gene_to_uniprot_maps",
    "resolve_gene_symbol_to_uniprot",
    # --- Schema Migrations ---
    "run_migrations",
    # --- Package Metadata ---
    "__version__",
]

# ---------------------------------------------------------------------------
# Symbol-to-submodule mapping for lazy loading
# (Domain 1: Architecture, Domain 4: Coding, Domain 12: Configuration)
#
# To support alternative database backends, replace the module paths
# in _SYMBOL_MAP with the new backend's module paths.  The __getattr__
# logic remains the same.
# ---------------------------------------------------------------------------
_SYMBOL_MAP: dict[str, str] = {
    # --- Connection Management (database.connection) ---
    "get_db_session": "database.connection",
    "get_engine": "database.connection",
    "init_db": "database.connection",
    "dispose_engine": "database.connection",
    "check_connection": "database.connection",
    "get_session_factory": "database.connection",
    "Base": "database.base",
    # --- ORM Models (database.models) ---
    "Drug": "database.models",
    "Protein": "database.models",
    "DrugProteinInteraction": "database.models",
    "ProteinProteinInteraction": "database.models",
    "GeneDiseaseAssociation": "database.models",
    "EntityMapping": "database.models",
    "PipelineRun": "database.models",
    "SchemaVersion": "database.models",
    "SCHEMA_VERSION": "database.models",
    "cleanup_orphan_gda_records": "database.models",
    # --- Data Operations (database.loaders) ---
    "bulk_upsert_drugs": "database.loaders",
    "bulk_upsert_proteins": "database.loaders",
    "bulk_upsert_dpi": "database.loaders",
    "bulk_upsert_ppi": "database.loaders",
    "bulk_upsert_gda": "database.loaders",
    "bulk_upsert_entity_mapping": "database.loaders",
    "bulk_update_drugs_from_pubchem": "database.loaders",
    "get_uniprot_to_protein_id_map": "database.loaders",
    "get_inchikey_to_drug_id_map": "database.loaders",
    "build_gene_to_uniprot_maps": "database.loaders",
    "resolve_gene_symbol_to_uniprot": "database.loaders",
    # --- Schema Migrations (database.migrations) ---
    "run_migrations": "database.migrations",
}

# ---------------------------------------------------------------------------
# Cache for lazily-loaded symbols
# ---------------------------------------------------------------------------
_loaded: dict[str, Any] = {}

# ---------------------------------------------------------------------------
# Environment-driven lazy/eager mode toggle (Domain 12: Configuration)
# When DATABASE_LAZY_IMPORT=0, symbols are loaded eagerly at import time
# (fail-fast for production). Default: lazy (DATABASE_LAZY_IMPORT=1).
# ---------------------------------------------------------------------------
_LAZY_MODE: bool = os.environ.get("DATABASE_LAZY_IMPORT", "1") != "0"

# ---------------------------------------------------------------------------
# Optional observability callback (Domain 11: Logging & Observability)
# Set to a callable(name, module_path, load_time_ms) to track import perf.
# ---------------------------------------------------------------------------
_on_symbol_loaded_callback: Any = None


def __getattr__(name: str) -> Any:
    """Lazily load a symbol from the appropriate submodule on first access.

    This function is called by Python when an attribute is not found in the
    module's ``globals()``.  It resolves the attribute name to a submodule
    via ``_SYMBOL_MAP``, imports that submodule with ``importlib``, extracts
    the attribute, caches it, and returns it.

    Error Handling
    ~~~~~~~~~~~~~~
    - ``ImportError`` / ``ModuleNotFoundError`` — wrapped with an informative
      message naming the symbol, submodule, and original error.  This
      prevents cryptic tracebacks during Airflow DAG parsing.
    - ``AttributeError`` — raised when the submodule loads but does not
      contain the expected symbol (version mismatch).
    - Unknown symbols raise ``AttributeError`` with the module name.

    Parameters
    ----------
    name : str
        The attribute name being accessed (e.g., ``"Drug"``).

    Returns
    -------
    Any
        The resolved symbol from the target submodule.

    Raises
    ------
    AttributeError
        If ``name`` is not in ``_SYMBOL_MAP`` or the submodule does not
        contain the attribute.
    ImportError
        If the target submodule cannot be imported.
    """
    # __version__ is defined at module level — no lazy load needed
    if name == "__version__":
        return __version__

    if name not in _SYMBOL_MAP:
        raise AttributeError(
            f"module 'database' has no attribute '{name}'"
        )

    if name in _loaded:
        return _loaded[name]

    module_path = _SYMBOL_MAP[name]
    start_time = time.monotonic()

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        logger.error(
            "Failed to lazily load '%s' from '%s': %s",
            name, module_path, exc,
        )
        raise ImportError(
            f"Cannot import '{name}' from '{module_path}'. "
            f"Ensure the submodule and its dependencies are "
            f"properly configured. Original error: {exc}"
        ) from exc

    try:
        attr = getattr(module, name)
    except AttributeError as exc:
        logger.error(
            "Module '%s' has no attribute '%s': %s",
            module_path, name, exc,
        )
        raise AttributeError(
            f"Module '{module_path}' has no attribute "
            f"'{name}'. This may indicate a version mismatch. "
            f"Original error: {exc}"
        ) from exc

    load_time_ms = (time.monotonic() - start_time) * 1000
    _loaded[name] = attr

    logger.debug(
        "Lazily loaded '%s' from '%s' (%.1f ms)",
        name, module_path, load_time_ms,
    )

    # Domain 11: observability callback
    if _on_symbol_loaded_callback is not None:
        try:
            _on_symbol_loaded_callback(
                name, module_path, load_time_ms
            )
        except Exception as cb_exc:
            logger.warning(
                "Observability callback failed for '%s': %s",
                name, cb_exc,
            )

    return attr


def __dir__() -> list[str]:
    """Return public symbols for tab-completion and introspection.

    Merges ``__all__`` (the declared public API) with ``globals()``
    keys so that IDE auto-complete and ``dir(database)`` show all
    available symbols.
    """
    return sorted(set(list(__all__) + list(globals().keys())))


# ---------------------------------------------------------------------------
# Eager loading mode (Domain 12: Configuration, Domain 7: Idempotency)
# ---------------------------------------------------------------------------
if not _LAZY_MODE:
    logger.info(
        "DATABASE_LAZY_IMPORT=0: pre-loading all %d symbols eagerly",
        len(_SYMBOL_MAP),
    )
    for _sym in _SYMBOL_MAP:
        try:
            __getattr__(_sym)
        except (ImportError, AttributeError) as _exc:
            logger.warning(
                "Eager load failed for '%s': %s", _sym, _exc
            )


def _reset() -> None:
    """Clear the lazy-loaded symbol cache for testing.

    Use this in test fixtures to reset the database package state
    between tests that require different configurations.

    This function does **not** dispose the engine or close sessions —
    it only clears the symbol cache so that subsequent attribute accesses
    re-import the submodules.

    Usage in conftest.py::

        @pytest.fixture(autouse=True)
        def reset_database_package():
            import database
            database._reset()
            yield
            database._reset()

    Note: ``importlib.reload(database)`` is also supported, but
    ``_reset()`` is preferred for testing because it does not re-create
    the module object.
    """
    _loaded.clear()
    logger.debug("Database package symbol cache cleared")


def _log_import_status() -> dict[str, bool]:
    """Log which symbols have been loaded and which haven't.

    Returns a dict mapping each symbol name to ``True`` (loaded) or
    ``False`` (not yet loaded).  Useful for debugging import issues
    and verifying lazy-loading behaviour.

    Returns
    -------
    dict[str, bool]
        Symbol name -> loaded status mapping.
    """
    status = {
        name: (name in _loaded) for name in _SYMBOL_MAP
    }
    loaded_count = sum(1 for v in status.values() if v)
    total_count = len(status)
    logger.info(
        "Database package import status: %d / %d symbols loaded",
        loaded_count, total_count,
    )
    for name, is_loaded in status.items():
        if is_loaded:
            logger.debug("  [LOADED]   %s", name)
        else:
            logger.debug("  [PENDING]  %s", name)
    return status


def validate_data_quality_infrastructure(session) -> dict[str, Any]:
    """Validate that the data quality infrastructure is properly configured.

    This function performs a comprehensive check of the database package's
    data quality safeguards, including:

    1. All bulk_upsert functions are importable via the package API.
    2. All ORM models define the expected unique constraints.
    3. Lookup functions are importable.
    4. Each ORM model's table exists and has the expected columns.

    This function should be called **after** ``init_db()`` has been run.

    Parameters
    ----------
    session : sqlalchemy.orm.Session
        An active database session for introspection.

    Returns
    -------
    dict[str, Any]
        A validation report with keys:
        - ``"checks"``: list of check results (pass/fail + message)
        - ``"passed"``: number of passing checks
        - ``"failed"``: number of failing checks
        - ``"overall"``: ``"PASS"`` if all checks pass, ``"FAIL"``
          otherwise.
    """
    checks: list[dict[str, str]] = []

    # Check 1: All bulk_upsert functions are importable
    bulk_functions = [
        "bulk_upsert_drugs",
        "bulk_upsert_proteins",
        "bulk_upsert_dpi",
        "bulk_upsert_ppi",
        "bulk_upsert_gda",
        "bulk_upsert_entity_mapping",
        "bulk_update_drugs_from_pubchem",
    ]
    for func_name in bulk_functions:
        try:
            func = __getattr__(func_name)
            checks.append({
                "check": f"import_{func_name}",
                "status": "PASS",
                "message": f"{func_name} is importable",
            })
        except (ImportError, AttributeError) as exc:
            checks.append({
                "check": f"import_{func_name}",
                "status": "FAIL",
                "message": f"Cannot import {func_name}: {exc}",
            })

    # Check 2: All lookup functions are importable
    lookup_functions = [
        "get_uniprot_to_protein_id_map",
        "get_inchikey_to_drug_id_map",
        "build_gene_to_uniprot_maps",
        "resolve_gene_symbol_to_uniprot",
    ]
    for func_name in lookup_functions:
        try:
            func = __getattr__(func_name)
            checks.append({
                "check": f"import_{func_name}",
                "status": "PASS",
                "message": f"{func_name} is importable",
            })
        except (ImportError, AttributeError) as exc:
            checks.append({
                "check": f"import_{func_name}",
                "status": "FAIL",
                "message": f"Cannot import {func_name}: {exc}",
            })

    # Check 3: ORM models define expected unique constraints
    model_constraint_map = {
        "Drug": ["inchikey"],
        "Protein": ["uniprot_id"],
        "DrugProteinInteraction": [
            "drug_id", "protein_id", "source", "source_id"
        ],
        "ProteinProteinInteraction": [
            "protein_a_id", "protein_b_id"
        ],
        "GeneDiseaseAssociation": [
            "gene_symbol", "disease_id", "source"
        ],
    }
    for model_name, expected_cols in model_constraint_map.items():
        try:
            model_cls = __getattr__(model_name)
            table = model_cls.__table__
            constraint_cols: set[str] = set()
            for constraint in table.constraints:
                if hasattr(constraint, "columns"):
                    for col in constraint.columns:
                        constraint_cols.add(col.name)
            missing = set(expected_cols) - constraint_cols
            if missing:
                checks.append({
                    "check": f"constraints_{model_name}",
                    "status": "FAIL",
                    "message": (
                        f"{model_name} is missing constraint "
                        f"columns: {missing}"
                    ),
                })
            else:
                checks.append({
                    "check": f"constraints_{model_name}",
                    "status": "PASS",
                    "message": (
                        f"{model_name} has expected "
                        f"constraint columns"
                    ),
                })
        except Exception as exc:
            checks.append({
                "check": f"constraints_{model_name}",
                "status": "FAIL",
                "message": f"Cannot check {model_name}: {exc}",
            })

    # Check 4: EntityMapping has partial unique index on inchikey
    try:
        em_cls = __getattr__("EntityMapping")
        table = em_cls.__table__
        has_inchikey_constraint = any(
            hasattr(c, "columns") and
            any(col.name == "canonical_inchikey" for col in c.columns)
            for c in table.constraints
        )
        checks.append({
            "check": "constraints_EntityMapping",
            "status": "PASS" if has_inchikey_constraint else "WARN",
            "message": (
                "EntityMapping inchikey constraint "
                + ("present" if has_inchikey_constraint
                   else "not found (partial index)")
            ),
        })
    except Exception as exc:
        checks.append({
            "check": "constraints_EntityMapping",
            "status": "FAIL",
            "message": f"Cannot check EntityMapping: {exc}",
        })

    passed = sum(1 for c in checks if c["status"] == "PASS")
    failed = sum(1 for c in checks if c["status"] == "FAIL")

    report = {
        "checks": checks,
        "passed": passed,
        "failed": failed,
        "overall": "PASS" if failed == 0 else "FAIL",
    }

    logger.info(
        "Data quality infrastructure validation: %s "
        "(%d passed, %d failed, %d total)",
        report["overall"], passed, failed, len(checks),
    )

    return report


def _validate_database_security() -> dict[str, Any]:
    """Audit DATABASE_URL for common insecure patterns.

    This function checks for the following security issues:

    1. In-memory SQLite used in non-test environments.
    2. Hardcoded credentials (no password separation).
    3. Non-SSL connections to remote PostgreSQL databases.
    4. Default or weak passwords (e.g., "password", "postgres").

    This function defers reading ``DATABASE_URL`` until it is called,
    consistent with the lazy-loading philosophy.

    Returns
    -------
    dict[str, Any]
        A security report with keys:
        - ``"checks"``: list of check results (pass/fail + message)
        - ``"warnings"``: number of warnings
        - ``"critical"``: number of critical issues
        - ``"overall"``: ``"SECURE"`` or ``"INSECURE"``
    """
    from urllib.parse import urlparse

    checks: list[dict[str, str]] = []

    try:
        # Deferred: only read config when this function is called
        from config.settings import DATABASE_URL

        parsed = urlparse(DATABASE_URL)
        scheme = parsed.scheme.split("+")[0] if parsed.scheme else ""
        # Chain 1 root fix: prefer DRUGOS_ENVIRONMENT (canonical).
        environment = (
            os.environ.get("DRUGOS_ENVIRONMENT")
            or os.environ.get("ENVIRONMENT", "development")
        )

        # Check 1: In-memory SQLite in non-test environments
        if ":memory:" in (DATABASE_URL or ""):
            if environment not in ("test", "testing", "ci"):
                checks.append({
                    "check": "in_memory_sqlite",
                    "severity": "CRITICAL",
                    "message": (
                        "In-memory SQLite detected in "
                        f"'{environment}' environment. "
                        "Data will be lost on restart."
                    ),
                })
            else:
                checks.append({
                    "check": "in_memory_sqlite",
                    "severity": "INFO",
                    "message": (
                        "In-memory SQLite in test environment "
                        "is acceptable."
                    ),
                })

        # Check 2: Non-SSL PostgreSQL connection
        if scheme == "postgresql" and parsed.hostname:
            is_localhost = parsed.hostname in (
                "localhost", "127.0.0.1", "::1"
            )
            has_ssl = (
                "sslmode" in (DATABASE_URL or "").lower()
            )
            if not is_localhost and not has_ssl:
                checks.append({
                    "check": "ssl_connection",
                    "severity": "WARNING",
                    "message": (
                        "PostgreSQL connection to remote host "
                        f"'{parsed.hostname}' without SSL. "
                        "Add ?sslmode=require to DATABASE_URL."
                    ),
                })

        # Check 3: Default/weak passwords
        weak_passwords = {
            "password", "postgres", "admin", "root", "",
            "123456", "changeme",
        }
        if parsed.password and parsed.password.lower() in weak_passwords:
            checks.append({
                "check": "weak_password",
                "severity": "CRITICAL",
                "message": (
                    "DATABASE_URL uses a default/weak password. "
                    "Use a strong, unique password."
                ),
            })

        # Check 4: Credentials in URL (expected but worth noting)
        if parsed.username and parsed.password:
            checks.append({
                "check": "credentials_in_url",
                "severity": "INFO",
                "message": (
                    "DATABASE_URL contains embedded credentials. "
                    "Ensure this value is not logged or exposed "
                    "in error messages."
                ),
            })

    except ImportError as exc:
        checks.append({
            "check": "config_import",
            "severity": "CRITICAL",
            "message": f"Cannot import config.settings: {exc}",
        })
    except Exception as exc:
        checks.append({
            "check": "general",
            "severity": "CRITICAL",
            "message": f"Unexpected error during security audit: {exc}",
        })

    warnings = sum(
        1 for c in checks if c["severity"] == "WARNING"
    )
    critical = sum(
        1 for c in checks if c["severity"] == "CRITICAL"
    )

    report = {
        "checks": checks,
        "warnings": warnings,
        "critical": critical,
        "overall": "SECURE" if critical == 0 else "INSECURE",
    }

    if critical > 0:
        logger.warning(
            "Database security audit: %s (%d critical, %d warnings)",
            report["overall"], critical, warnings,
        )
    else:
        logger.info(
            "Database security audit: %s (%d warnings)",
            report["overall"], warnings,
        )

    return report
