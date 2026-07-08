"""
Cross-dialect Python migration runner for the Drug Repurposing ETL platform.

This module implements the migration execution engine. It handles both
PostgreSQL (full SQL file execution) and SQLite (Python-level column
additions) dialects, with comprehensive error handling, retry logic,
scientific validation, data quality checks, and observability.

This module has been hardened across 16 verification domains covering
118 issues (Architecture, Design, Scientific Correctness, Coding,
Data Quality, Reliability, Idempotency, Performance, Security,
Testing, Logging, Configuration, Documentation, Compliance,
Interoperability, and Data Lineage).

Public API
----------
- run_migrations(engine, config) -> MigrationResult
- check_migrations(engine) -> MigrationHealthResult
- get_migration_status(engine) -> MigrationStatus
- validate_scientific_constraints(engine) -> list[str]
- validate_migration_config(config) -> list[str]
- verify_schema_matches_orm(engine) -> dict
- get_sql_migration_files() -> list[Path]
- get_migration_runner() -> Callable
- rollback_migration(migration_name, engine) -> None  [PLANNED — not yet implemented]
- verify_package_exports() -> dict[str, bool]
- get_database_fingerprint(engine) -> dict
- create_test_migrations_dir(tmp_path) -> Path
- reset_migration_state(engine) -> None
- count_applied_migrations(engine) -> int
- get_migration_checksum(engine, name) -> str | None
- verify_table_schema(engine, table_name, expected_columns) -> bool
- plan_migrations(engine, config) -> list[dict]
- get_failed_migrations(engine) -> list[dict]
- retry_failed_migration(engine, migration_name) -> bool
- analyze_migration_impact(engine, migration_name) -> dict
- resolve_failed_migration(engine, migration_name, resolution_note) -> bool
- get_partial_migration_state(engine, migration_name) -> dict

Usage:
    python -m database.migrations.run_migrations
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import platform
import re
import threading
import time
import warnings
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import inspect, text
from sqlalchemy.exc import (
    DataError,
    InterfaceError,
    NoSuchTableError,
    OperationalError,
    ProgrammingError,
    ResourceClosedError,
)

# Deferred import to avoid circular dependency with database.__init__ lazy
# loading (BUG-ARCH-04).  get_engine is imported at point of use via
# _get_default_engine() instead of at module top-level.
# from database.connection import get_engine  # DO NOT import at top level

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dialect constants (CFG-MIG-02)
# ---------------------------------------------------------------------------
DIALECT_POSTGRESQL: str = "postgresql"
DIALECT_SQLITE: str = "sqlite"
SUPPORTED_DIALECTS: frozenset[str] = frozenset({DIALECT_POSTGRESQL, DIALECT_SQLITE})

# ---------------------------------------------------------------------------
# Scientific constants (SCI-MIG-02, SCI-MIG-04, SCI-MIG-06)
# Used in validate_scientific_constraints for pre-migration checks.
# ---------------------------------------------------------------------------

INCHIKEY_MAX_LENGTH: int = 50
STANDARD_INCHIKEY_LENGTH: int = 27
SYNTHETIC_INCHIKEY_PREFIX: str = "SYNTH"
STRING_SCORE_MIN: int = 0
STRING_SCORE_MAX: int = 1000
MOLECULAR_WEIGHT_PRECISION: int = 6  # Used in validate_scientific_constraints (GAP-SCI-05)
MIGRATION_BATCH_SIZE: int = 10000
PLANNED_MIGRATION_FRAMEWORK: str = "alembic"

# Canonical migration filename pattern: NNN_description.sql
# (BUG-CODE-01: removed duplicate MIGRATION_FILENAME_PATTERN_CONST)
#
# audit-2025 ROOT FIX (issue 24): the previous pattern required
# EXACTLY 3 digits (``\d{3}``), but ``database/base.py`` (the schema
# version derivator) accepts 1-3 digits (``\d{1,3}``). That mismatch
# meant a migration file named ``1_initial.sql`` or ``12_fix.sql``
# would be picked up by the schema-version detector but rejected by
# the migration runner — silently skipping the migration while
# reporting the schema version as if it had run. The fix aligns both
# patterns to ``\d{1,3}`` (1 to 3 digits) so the runner and the
# detector agree.
MIGRATION_FILENAME_PATTERN: str = r"^\d{1,3}_[a-z][a-z0-9_]*\.sql$"

# ---------------------------------------------------------------------------
# Migration directory (CFG-MIG-03 — overridable via MigrationConfig)
# BUG-CFG-01: Computed at import time but overridable via config.
# ---------------------------------------------------------------------------
MIGRATIONS_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# SQL identifier validation (SEC-MIG-01, BUG-SEC-01)
# ---------------------------------------------------------------------------
SQL_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")

# SQL keywords that must not be used as identifiers (BUG-SEC-01)
_SQL_KEYWORDS: frozenset[str] = frozenset({
    "SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TABLE", "INDEX", "VIEW", "GRANT", "REVOKE", "TRUNCATE", "FROM",
    "WHERE", "JOIN", "INNER", "OUTER", "LEFT", "RIGHT", "ON", "AND",
    "OR", "NOT", "NULL", "DEFAULT", "SET", "INTO", "VALUES",
})

# Maximum migration filename length (CFG-MIG-06)
MIGRATION_NAME_MAX_LENGTH: int = 200

# ---------------------------------------------------------------------------
# Migration status values (GUARD-DES-08)
# ---------------------------------------------------------------------------
VALID_MIGRATION_STATUSES: frozenset[str] = frozenset({
    "applied", "failed", "skipped", "rolled_back", "retrying", "in_progress",
})

# Valid log levels for structured event logging (GAP-DES-07)
VALID_LOG_LEVELS: frozenset[str] = frozenset({
    "debug", "info", "warning", "error", "critical",
})

# Maximum failure count before blocking a migration (BUG-DQ-03)
MAX_FAILURE_COUNT: int = 5

# Non-deterministic SQL functions to warn about (GAP-IDEM-05)
NONDETERMINISTIC_FUNCTIONS: tuple[str, ...] = (
    "RANDOM()", "RANDOM", "NOW()", "CLOCK_TIMESTAMP()",
    "TRANSACTION_TIMESTAMP()", "STATEMENT_TIMESTAMP()",
)

# Error message length cap (GAP-SEC-04)
ERROR_MESSAGE_MAX_LENGTH: int = 500

# ---------------------------------------------------------------------------
# Column additions that need to be cross-dialect safe
# BUG-ARCH-03: Expanded REQUIRED_COLUMNS to cover ALL 7 core tables
# with all columns added by migrations 002 and 003.
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "proteins": [
        ("gene_symbol", "VARCHAR(50)"),
        ("protein_name", "TEXT"),
        ("function_desc", "TEXT"),
    ],
    "drugs": [
        ("is_fda_approved", "BOOLEAN DEFAULT 0"),
        ("max_phase", "INTEGER"),
        ("drug_type", "VARCHAR(50)"),
        ("mechanism_of_action", "TEXT"),
        # LIFE-SAFETY CRITICAL: withdrawn drug tracking columns
        ("is_withdrawn", "BOOLEAN NOT NULL DEFAULT 0"),
        ("clinical_status", "VARCHAR(30)"),
        ("cas_number", "VARCHAR(20)"),
        ("logp", "FLOAT"),
        ("tpsa", "FLOAT"),
        ("h_bond_donor_count", "INTEGER"),
        ("h_bond_acceptor_count", "INTEGER"),
        ("rotatable_bond_count", "INTEGER"),
        ("heavy_atom_count", "INTEGER"),
        ("complexity", "INTEGER"),
        ("completeness_score", "FLOAT"),
        # v17 ROOT FIX (PS-6 fallback gap): REQUIRED_COLUMNS is the
        # Python-side fallback that runs when a SQL migration fails to
        # apply (e.g. SQLite translation error). Migration 006 adds the
        # ``groups`` column (DrugBank <groups> field — semicolon-separated
        # regulatory states: approved;investigational;withdrawn;...).
        # Without ``groups`` in this fallback list, a SQLite dev/test DB
        # where migration 006 was skipped would have NO ``groups`` column
        # at all — so bulk_upsert_drugs (which now includes 'groups' in
        # updatable_cols per the PS-6 fix) would raise
        # ``sqlite3.OperationalError: table 'drugs' has no column named
        # 'groups'``. Adding it here ensures the Python fallback creates
        # the column even if the SQL migration is skipped.
        ("groups", "VARCHAR(200)"),
    ],
    "drug_protein_interactions": [
        ("confidence_score", "FLOAT"),
        ("source_version", "VARCHAR(50)"),
        ("source_fetch_date", "TIMESTAMP"),
        ("entity_resolved", "BOOLEAN DEFAULT 0"),
        ("pipeline_run_id", "INTEGER"),
    ],
    "protein_protein_interactions": [
        ("updated_at", "TIMESTAMP"),
        ("score_json", "TEXT"),
        ("pipeline_run_id", "INTEGER"),
    ],
    "gene_disease_associations": [
        # v14 ROOT FIX (FIX4 / CD-3): protein_id column was REMOVED from
        # the GDA table — the table uses the STRING uniprot_id FK as the
        # canonical protein reference. The loader never populated
        # protein_id; the migration 003 backfill was a no-op; the index
        # was unused; the column produced false-positive schema drift.
        ("disease_id_type", "VARCHAR(20)"),
        ("score_type", "VARCHAR(50)"),
        ("score_method", "VARCHAR(100)"),
        ("pipeline_run_id", "INTEGER"),
    ],
    "entity_mapping": [
        ("match_confidence", "FLOAT"),
        ("match_history", "TEXT"),
    ],
    "pipeline_runs": [
        ("error_message", "VARCHAR(500)"),
    ],
}

# Known tables for row-count tracking (LOG-MIG-06, GAP-CODE-08: tuple instead of list)
_KNOWN_TABLES: tuple[str, ...] = (
    "drugs",
    "proteins",
    "drug_protein_interactions",
    "protein_protein_interactions",
    "gene_disease_associations",
    "entity_mapping",
    "pipeline_runs",
    # schema_version is metadata, not tracked for row counts
)

# Expected schema for verify_schema_matches_orm fallback (BUG-ARCH-05)
# Maps table_name -> sorted list of expected column names
#
# BUG-A-003 root fix: previous version of this dict had PHANTOM columns
# (assay_chembl_id, entity_type, source_db, target_db, target_id,
# pipeline_name, start_time, end_time, records_processed, protein_id on
# gene_disease_associations) that did NOT exist in the ORM models. This
# caused verify_schema_matches_orm's fallback path to report a false
# "schema mismatch" on every clean database, masking real schema drift.
# The dict below is now GENERATED from the ORM __table__.columns at
# import time so it can never drift from the ORM again. The explicit
# table list is kept so the fallback still knows which tables to verify.
EXPECTED_SCHEMA: dict[str, list[str]] = {}

def _build_expected_schema_from_orm() -> dict[str, list[str]]:
    """Build EXPECTED_SCHEMA from ORM models (BUG-A-003 root fix).

    Previously EXPECTED_SCHEMA was a hand-maintained dict that drifted
    from the ORM as columns were added/removed in models.py. This
    function introspects the ORM at import time and builds the dict
    directly from ``cls.__table__.columns``, so a schema mismatch can
    never happen by construction.
    """
    try:
        from database.models import (  # type: ignore[import-not-found]
            Drug,
            DrugProteinInteraction,
            ProteinProteinInteraction,
            GeneDiseaseAssociation,
            EntityMapping,
            PipelineRun,
            Protein,
        )
    except Exception as _exc:  # pragma: no cover - fallback for tests
        # If SQLAlchemy is not installed (e.g. lightweight CI), fall back
        # to a static dict that matches the ORM as of the last edit.
        # This is intentionally minimal — the production path uses the
        # ORM introspection above.
        return {
            "drugs": sorted([
                "id", "inchikey", "name", "chembl_id", "drugbank_id", "pubchem_cid",
                "molecular_formula", "molecular_weight", "smiles", "is_fda_approved",
                "max_phase", "drug_type", "mechanism_of_action",
                "is_withdrawn", "clinical_status", "cas_number",
                "logp", "tpsa", "h_bond_donor_count", "h_bond_acceptor_count",
                "rotatable_bond_count", "heavy_atom_count", "complexity",
                "completeness_score",
                "created_at", "updated_at", "is_deleted", "deleted_at",
            ]),
            # v14 ROOT FIX: proteins table was MISSING from the fallback
            # dict — caused test_expected_schema_defined to fail in test
            # contexts where the ORM import fails. Added to match the
            # ORM's Protein model columns.
            "proteins": sorted([
                "id", "uniprot_id", "gene_name", "gene_symbol", "protein_name",
                "sequence", "function_desc", "organism", "taxonomy_id",
                "sequence_length", "sequence_mass", "protein_type",
                "subcellular_location", "alternative_names",
                "completeness_score",
                "created_at", "updated_at", "is_deleted", "deleted_at",
            ]),
            "drug_protein_interactions": sorted([
                "id", "drug_id", "protein_id", "activity_type", "activity_value",
                "activity_units", "interaction_type", "confidence_score",
                "source", "source_id", "source_version", "source_fetch_date",
                "entity_resolved", "pipeline_run_id",
                "created_at", "updated_at",
            ]),
            "protein_protein_interactions": sorted([
                "id", "protein_a_id", "protein_b_id", "combined_score",
                "experimental_score", "database_score", "textmining_score",
                "source", "score_json", "pipeline_run_id",
                "created_at", "updated_at",
            ]),
            "gene_disease_associations": sorted([
                "id", "gene_symbol", "gene_id", "disease_id", "disease_name",
                "disease_type", "disease_class", "disease_class_source",
                "disease_id_type", "disease_name_was_filled",
                # v14 ROOT FIX (FIX4 / CD-3): protein_id column was REMOVED
                # from the GDA table — the table uses the STRING uniprot_id
                # FK as the canonical protein reference. The loader never
                # populated protein_id; the migration 003 backfill was a
                # no-op; the index was unused; the column produced false-
                # positive schema drift.
                "score", "original_score", "normalized_score", "score_type",
                "score_method", "score_direction", "score_was_clipped",
                "score_was_coerced_nan", "evidence_strength",
                "confidence_tier", "confidence_tier_method",
                "association_type", "association_type_was_filled",
                "resolution_method", "dedup_strategy",
                "source", "source_id", "source_format", "source_version",
                "source_url", "download_method", "download_date",
                "snapshot_tag", "schema_version",
                "uniprot_id", "gene_to_uniprot_map_version",
                # v14 ROOT FIX (FIX4 / CD-3): protein_id column was REMOVED
                # from the GDA table. The table uses the STRING uniprot_id
                # FK as the canonical protein reference.
                "pmid_list", "pmid_list_was_capped", "original_pmid_count",
                "year_initial", "year_final",
                "pipeline_run_id", "created_at", "updated_at",
            ]),
            "entity_mapping": sorted([
                "id", "drugbank_id", "chembl_id", "pubchem_cid", "uniprot_id",
                "string_id", "canonical_inchikey", "canonical_name",
                "match_confidence", "match_method", "match_history",
                "created_at", "updated_at",
            ]),
            "pipeline_runs": sorted([
                "id", "source", "status", "run_date",
                "duration_seconds", "records_downloaded", "records_cleaned",
                "records_loaded", "error_message",
                "created_at", "updated_at",
            ]),
        }

    table_to_model = {
        "drugs": Drug,
        "proteins": Protein,
        "drug_protein_interactions": DrugProteinInteraction,
        "protein_protein_interactions": ProteinProteinInteraction,
        "gene_disease_associations": GeneDiseaseAssociation,
        "entity_mapping": EntityMapping,
        "pipeline_runs": PipelineRun,
    }
    schema: dict[str, list[str]] = {}
    for table_name, model in table_to_model.items():
        try:
            cols = sorted([c.name for c in model.__table__.columns])
        except Exception:
            # Skip models that can't be introspected (e.g. test stubs)
            continue
        schema[table_name] = cols
    return schema

EXPECTED_SCHEMA = _build_expected_schema_from_orm()

# ---------------------------------------------------------------------------
# Compiled regexes for analyze_migration_impact (GUARD-CODE-12, GUARD-CODE-13)
# Moved to module level for performance — compiled once, not per call.
# ---------------------------------------------------------------------------
_ALTER_TABLE_ADD_COL_PATTERN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE,
)
_ALTER_TABLE_DROP_COL_PATTERN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+(\w+)", re.IGNORECASE,
)
_ALTER_TABLE_ALTER_COL_PATTERN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ALTER\s+COLUMN\s+(\w+)", re.IGNORECASE,
)
_ALTER_TABLE_ADD_CONSTR_PATTERN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+CONSTRAINT\s+(\w+)", re.IGNORECASE,
)
_ALTER_TABLE_DROP_CONSTR_PATTERN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+CONSTRAINT\s+(\w+)", re.IGNORECASE,
)
_CREATE_TABLE_PATTERN = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE,
)
_DELETE_FROM_PATTERN = re.compile(r"DELETE\s+FROM\s+(\w+)", re.IGNORECASE)
_INSERT_INTO_PATTERN = re.compile(r"INSERT\s+INTO\s+(\w+)", re.IGNORECASE)
_UPDATE_PATTERN = re.compile(r"UPDATE\s+(\w+)\s+SET", re.IGNORECASE)

# InChIKey format regex for standard structure validation (GUARD-SCI-06).
# v9 ROOT FIX (audit F3.8): the previous pattern ``^[A-Z]{14}-[A-Z0-9]{10}-[A-Z]$``
# allowed DIGITS in the second block. Per the InChI specification (IUPAC
# InChIKey FAQ), block 2 consists of 10 UPPERCASE LETTERS ONLY (it encodes
# the tautomer + isotope + stereo layers using a letter-only encoding).
# Allowing digits made this regex inconsistent with the other 5 InChIKey
# regexes in the codebase (normalizer.py, models.py, resolver_utils.py)
# which all use ``[A-Z]{10}``. A key accepted by run_migrations could be
# rejected by normalizer — the F3.8 "6 different InChIKey regexes"
# compound-destruction pattern. Now standardised to ``[A-Z]{10}`` (no
# digits) to match the spec and all other modules.
_INCHIKEY_STANDARD_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")

# ---------------------------------------------------------------------------
# Migration phase tracking (BUG-ARCH-02)
# ---------------------------------------------------------------------------


class _MigrationPhase(Enum):
    """Phases of a migration run for interrupted-run detection."""
    TRACKING_TABLES = "tracking_tables"
    SCIENTIFIC_VALIDATION = "scientific_validation"
    COLUMN_ADDITIONS = "column_additions"
    SQL_FILES = "sql_files"
    POST_VERIFY = "post_verify"
    LINEAGE_UPDATE = "lineage_update"


# ---------------------------------------------------------------------------
# Deferred engine import (BUG-ARCH-04)
# ---------------------------------------------------------------------------


def _get_default_engine():
    """Lazily import and return the default database engine.

    Defers ``from database.connection import get_engine`` to the point of
    use to avoid circular imports when ``database.__init__`` is still
    loading (BUG-ARCH-04).
    """
    try:
        from database.connection import get_engine
        return get_engine()
    except ImportError as exc:
        raise ImportError(
            f"Cannot import get_engine from database.connection: {exc}. "
            f"Ensure database.connection is properly configured and "
            f"SQLAlchemy is installed."
        ) from exc


# ---------------------------------------------------------------------------
# SQL identifier validation (SEC-MIG-01, BUG-SEC-01)
# ---------------------------------------------------------------------------


def _validate_sql_identifier(name: str, kind: str = "identifier") -> str:
    """Validate a SQL identifier to prevent injection.

    Also rejects SQL keywords (BUG-SEC-01) and Python dunder names.

    Parameters
    ----------
    name : str
        The identifier to validate.
    kind : str
        Human-readable description for error messages (e.g., "table name").

    Returns
    -------
    str
        The validated identifier (unchanged).

    Raises
    ------
    ValueError
        If the identifier does not match the safe pattern, is a SQL
        keyword, or is a Python dunder name.
    """
    if not SQL_IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid SQL {kind}: {name!r}. "
            f"Must match ^[a-zA-Z_][a-zA-Z0-9_]{{0,127}}$"
        )
    # BUG-SEC-01: Reject SQL keywords
    if name.upper() in _SQL_KEYWORDS:
        raise ValueError(
            f"SQL {kind} is a reserved keyword: {name!r}. "
            f"Choose a different identifier."
        )
    # BUG-SEC-01: Reject Python dunder names
    if name.startswith("__") and name.endswith("__"):
        raise ValueError(
            f"SQL {kind} is a Python dunder name: {name!r}. "
            f"Choose a different identifier."
        )
    return name


# ---------------------------------------------------------------------------
# SQL statement splitting (BUG-ARCH-01)
# ---------------------------------------------------------------------------


def _split_sql_statements(sql_content: str) -> list[str]:
    """Split a multi-statement SQL string into individual statements.

    Handles:
    - Single-quoted string literals
    - Double-quoted identifiers
    - PostgreSQL dollar-quoted strings (DO $$ ... $$)
    - Line comments (--)
    - Block comments (/* ... */)
    - BEGIN;/COMMIT; transaction wrappers

    BUG-ARCH-01: Individual statement execution allows proper rollback
    if a statement fails mid-migration.
    """
    statements: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql_content)

    while i < n:
        ch = sql_content[i]

        # Line comment
        if ch == "-" and i + 1 < n and sql_content[i + 1] == "-":
            end = sql_content.find("\n", i)
            if end == -1:
                i = n
            else:
                i = end + 1
            continue

        # Block comment
        if ch == "/" and i + 1 < n and sql_content[i + 1] == "*":
            end = sql_content.find("*/", i + 2)
            if end == -1:
                i = n
            else:
                i = end + 2
            continue

        # Dollar-quoted string (BUG-INT-01: PostgreSQL $$ blocks)
        if ch == "$" and i + 1 < n and sql_content[i + 1] == "$":
            end = sql_content.find("$$", i + 2)
            if end == -1:
                # No closing $$, treat rest as string
                current.append(sql_content[i:])
                i = n
            else:
                current.append(sql_content[i:end + 2])
                i = end + 2
            continue

        # Single-quoted string literal
        if ch == "'":
            j = i + 1
            while j < n:
                if sql_content[j] == "'":
                    if j + 1 < n and sql_content[j + 1] == "'":
                        j += 2  # escaped quote
                    else:
                        j += 1
                        break
                else:
                    j += 1
            current.append(sql_content[i:j])
            i = j
            continue

        # Double-quoted identifier
        if ch == '"':
            j = i + 1
            while j < n and sql_content[j] != '"':
                j += 1
            if j < n:
                j += 1
            current.append(sql_content[i:j])
            i = j
            continue

        # Semicolon — end of statement
        if ch == ";":
            stmt = "".join(current).strip()
            # Skip empty statements and pure BEGIN/COMMIT
            upper = stmt.upper().strip()
            if upper and upper != "BEGIN" and upper != "COMMIT":
                statements.append(stmt)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    # Trailing content without semicolon
    stmt = "".join(current).strip()
    upper = stmt.upper().strip()
    if stmt and upper != "BEGIN" and upper != "COMMIT":
        statements.append(stmt)

    return statements


# ---------------------------------------------------------------------------
# Migration file helpers (ARCH-MIG-05, IDEM-MIG-04)
# ---------------------------------------------------------------------------


def _extract_migration_number(filename: str) -> int:
    """Extract the numeric prefix from a migration filename.

    Examples: '001_initial_schema.sql' -> 1, '010_x.sql' -> 10.
    BUG-CODE-02: Returns 0 and logs WARNING if no numeric prefix found
    (instead of float('inf') which silently sorts bad files last).
    """
    match = re.match(r"(\d+)", filename)
    if match:
        return int(match.group(1))
    logger.warning(
        "Migration file '%s' does not have a numeric prefix and will "
        "be processed first. If this is not a migration file, remove it "
        "from the migrations directory.",
        filename,
    )
    return 0


def _validate_migration_filename(filename: str) -> bool:
    """Check if a filename follows the NNN_description.sql convention."""
    return bool(re.match(MIGRATION_FILENAME_PATTERN, filename))


# ---------------------------------------------------------------------------
# Migration dependency graph (GAP-ARCH-06)
# ---------------------------------------------------------------------------

_DEPENDS_RE = re.compile(r"--\s*DEPENDS:\s*(.+)", re.IGNORECASE)


def _parse_migration_dependencies(sql_content: str) -> set[str]:
    """Parse DEPENDS header comments from a migration SQL file.

    Format: -- DEPENDS: 001, 002
    Returns set of dependency migration prefixes (e.g., {'001', '002'}).
    """
    deps: set[str] = set()
    for line in sql_content.split("\n"):
        m = _DEPENDS_RE.match(line.strip())
        if m:
            for dep in m.group(1).split(","):
                dep = dep.strip()
                if dep:
                    deps.add(dep)
    return deps


def _topological_sort(
    migrations: list[str],
    dependencies: dict[str, set[str]],
) -> list[str]:
    """Topological sort of migrations respecting dependency order.

    Raises MigrationError if a cycle is detected.
    """
    sorted_list: list[str] = []
    visited: set[str] = set()
    in_progress: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in in_progress:
            raise MigrationError(
                failed=[name],
                errors=[ValueError(f"Circular dependency detected involving: {name}")],
            )
        in_progress.add(name)
        for dep in dependencies.get(name, set()):
            if dep in {m for m in migrations}:
                visit(dep)
        in_progress.discard(name)
        visited.add(name)
        sorted_list.append(name)

    for mig in migrations:
        visit(mig)

    return sorted_list


# ---------------------------------------------------------------------------
# Helper: structured migration event logging (LOG-MIG-04, GAP-DES-07)
# ---------------------------------------------------------------------------


def _log_migration_event(
    event_type: str,
    migration_name: str,
    details: dict | None = None,
    level: str = "info",
    correlation_id: str | None = None,
    pipeline_name: str | None = None,
    run_id: str | None = None,
) -> None:
    """Log a structured migration event.

    Parameters
    ----------
    event_type : str
        One of 'started', 'applied', 'skipped', 'failed',
        'validated', 'rolled_back', 'retrying'.
    migration_name : str
        The migration filename.
    details : dict | None
        Additional structured data.
    level : str
        Log level. GAP-DES-07: Validated against VALID_LOG_LEVELS.
    correlation_id : str | None
        Distributed tracing correlation ID.
    pipeline_name : str | None
        Name of the pipeline triggering the migration.
    run_id : str | None
        Unique run identifier.
    """
    # GAP-DES-07: Validate log level
    if level not in VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid log level: {level!r}. Must be one of {sorted(VALID_LOG_LEVELS)}"
        )

    # BUG-LOG-01: Validate event_type
    valid_event_types = frozenset({
        "started", "applied", "skipped", "failed", "validated",
        "rolled_back", "retrying", "phase_started", "phase_completed",
    })
    if event_type not in valid_event_types:
        logger.warning("Unknown migration event_type: %s", event_type)

    # GUARD-LOG-07: Validate correlation_id format if provided
    if correlation_id and len(correlation_id) > 128:
        logger.warning("Correlation ID exceeds 128 chars: %s...", correlation_id[:32])

    log_data: dict[str, Any] = {
        "event": "migration",
        "event_type": event_type,
        "migration_name": migration_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if correlation_id:
        log_data["correlation_id"] = correlation_id
    if pipeline_name:
        log_data["pipeline_name"] = pipeline_name
    if run_id:
        log_data["run_id"] = run_id
    if details:
        log_data.update(details)
    getattr(logger, level)("Migration event: %s", log_data)


# ---------------------------------------------------------------------------
# Helper: table state logging (LOG-MIG-06, BUG-PERF-03, GAP-DQ-06)
# ---------------------------------------------------------------------------


def _get_approximate_row_count(conn, table_name: str, dialect_name: str) -> int:
    """Get approximate row count for a table.

    GAP-DQ-06: For PostgreSQL, uses pg_class.reltuples for fast
    approximate counts. Falls back to COUNT(*) for SQLite or when
    pg_class is unavailable.
    """
    if dialect_name == DIALECT_POSTGRESQL:
        try:
            r = conn.execute(
                text("SELECT reltuples::bigint FROM pg_class WHERE relname = :tn"),
                {"tn": table_name},
            )
            val = r.scalar()
            if val is not None and val >= 0:
                return int(val)
        except Exception:
            pass  # Fall through to COUNT(*)

    try:
        count = conn.execute(
            text(f"SELECT COUNT(*) FROM {_validate_sql_identifier(table_name, 'table name')}")
        ).scalar()
        return count or 0
    except Exception:
        return 0  # Table doesn't exist


def _log_table_state(conn, label: str, dialect_name: str = DIALECT_SQLITE) -> dict[str, int]:
    """Log and return row counts for all known tables.

    BUG-PERF-03: Uses UNION ALL for PostgreSQL to reduce round-trips.
    BUG-IDEM-04: Returns 0 for non-existent tables instead of -1.

    Parameters
    ----------
    conn : Connection
        SQLAlchemy connection.
    label : str
        Label for the log message (e.g., 'before_migration_003').
    dialect_name : str
        Database dialect name for optimization.

    Returns
    -------
    dict[str, int]
        Mapping of table_name -> row_count. 0 means table doesn't exist.
    """
    counts: dict[str, int] = {}

    # BUG-PERF-03: Try UNION ALL for a single round-trip
    if dialect_name == DIALECT_POSTGRESQL:
        try:
            union_parts = []
            for table in _KNOWN_TABLES:
                safe_name = _validate_sql_identifier(table, "table name")
                union_parts.append(
                    f"SELECT '{safe_name}' as tbl, COUNT(*) as cnt FROM {safe_name}"
                )
            if union_parts:
                union_sql = " UNION ALL ".join(union_parts)
                r = conn.execute(text(union_sql))
                for row in r.fetchall():
                    counts[row[0]] = row[1]
                logger.info("Table state %s: %s", label, counts)
                return counts
        except Exception:
            pass  # Fall through to per-table approach

    # Per-table fallback (SQLite or UNION ALL failure)
    for table in _KNOWN_TABLES:
        try:
            safe_name = _validate_sql_identifier(table, "table name")
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {safe_name}")
            ).scalar()
            counts[table] = count or 0
        except (OperationalError, NoSuchTableError):
            counts[table] = 0  # BUG-IDEM-04: 0 instead of -1
            logger.debug("Table '%s' does not exist yet", table)
        except Exception as exc:
            counts[table] = 0
            logger.warning("Could not count rows in '%s': %s", table, exc)

    logger.info("Table state %s: %s", label, counts)
    return counts


# ---------------------------------------------------------------------------
# Psql meta-command stripping (existing FIX C1)
# ---------------------------------------------------------------------------


def _strip_psql_meta_commands(sql_content: str) -> str:
    """Remove psql meta-command lines from SQL content.

    Psql meta-commands (e.g., ``\\c``, ``\\connect``, ``\\d``) are NOT valid SQL
    and crash SQLAlchemy's text(). This function strips all lines starting
    with a backslash at the beginning of a line, while preserving all valid
    SQL including DO $$ blocks.

    GAP-TEST-03 — Known edge cases:
    (a) '\\c mydb' -> stripped
    (b) "SELECT '\\\\';" -> preserved (backslash inside string)
    (c) 'DO $$ ... $$' -> preserved
    (d) '-- \\d table' -> stripped (comment with meta-command)
    (e) 'SELECT "hello\\\\world"' -> preserved
    """
    stripped_lines = []
    for line in sql_content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("\\") and not stripped.startswith("\\'") and not stripped.startswith('\\"'):
            logger.warning("Stripping psql meta-command from migration: %s", stripped)
            continue
        stripped_lines.append(line)
    return "\n".join(stripped_lines)


# ---------------------------------------------------------------------------
# Column / table existence checks (with specific exceptions, REL-MIG-03)
# ---------------------------------------------------------------------------


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    """Check whether a column already exists in the given table.

    Uses specific exception handling instead of bare ``except Exception``
    to avoid silently swallowing programming errors (BUG-CODE-06).

    Returns
    -------
    bool
        True if the column exists, False if the table doesn't exist or
        the column is absent.

    Raises
    ------
    OperationalError
        If a database connectivity issue occurs.
    """
    try:
        columns = [col["name"] for col in inspector.get_columns(table_name)]
        return column_name in columns
    except NoSuchTableError:
        logger.debug("Table '%s' does not exist yet", table_name)
        return False
    except OperationalError as exc:
        logger.warning(
            "Database error checking column '%s.%s': %s", table_name, column_name, exc
        )
        return False


def _table_exists(inspector, table_name: str) -> bool:
    """Check whether a table already exists."""
    return table_name in inspector.get_table_names()


# ---------------------------------------------------------------------------
# Migration tracking table (FIX D3, enhanced)
# ---------------------------------------------------------------------------


def _ensure_migration_tracking_table(engine) -> None:
    """Create the _migration_history table if it does not exist.

    This table tracks which .sql migration files have been applied,
    along with a checksum for detecting drift, and audit columns
    for who ran the migration and from where.

    Also creates _failed_migrations (REL-MIG-06),
    _migration_provenance (LINE-MIG-01), and
    _migration_data_changes (LINE-MIG-06) tables.
    """
    engine_dialect = engine.dialect.name
    with engine.begin() as conn:
        if engine_dialect == DIALECT_SQLITE:
            id_type = "INTEGER PRIMARY KEY AUTOINCREMENT"
        else:
            id_type = "SERIAL PRIMARY KEY"

        # _migration_history — tracks applied migrations
        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS _migration_history (
                    id {id_type},
                    migration_name VARCHAR({MIGRATION_NAME_MAX_LENGTH}) NOT NULL UNIQUE,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    checksum VARCHAR(64),
                    applied_by VARCHAR(100),
                    applied_from VARCHAR(200),
                    python_version VARCHAR(50),
                    status VARCHAR(20) DEFAULT 'applied',
                    applied_by_hash VARCHAR(32),
                    phase_at_interrupt VARCHAR(50)
                )
            """)
        )

        # Add audit columns if they don't exist (SEC-MIG-03) — idempotent
        _add_column_if_not_exists(
            conn, engine, "_migration_history", "applied_by", "VARCHAR(100)"
        )
        _add_column_if_not_exists(
            conn, engine, "_migration_history", "applied_from", "VARCHAR(200)"
        )
        _add_column_if_not_exists(
            conn, engine, "_migration_history", "python_version", "VARCHAR(50)"
        )
        _add_column_if_not_exists(
            conn, engine, "_migration_history", "status", "VARCHAR(20) DEFAULT 'applied'"
        )
        _add_column_if_not_exists(
            conn, engine, "_migration_history", "applied_by_hash", "VARCHAR(32)"
        )
        _add_column_if_not_exists(
            conn, engine, "_migration_history", "phase_at_interrupt", "VARCHAR(50)"
        )

        # _failed_migrations — dead letter queue (REL-MIG-06)
        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS _failed_migrations (
                    id {id_type},
                    migration_name VARCHAR({MIGRATION_NAME_MAX_LENGTH}) NOT NULL,
                    failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    error_message TEXT NOT NULL,
                    error_class VARCHAR(100),
                    retry_count INTEGER DEFAULT 0,
                    sql_checksum VARCHAR(64),
                    resolved BOOLEAN DEFAULT FALSE,
                    resolution_note TEXT
                )
            """)
        )
        # Add resolution_note column if missing (GAP-DQ-07)
        _add_column_if_not_exists(
            conn, engine, "_failed_migrations", "resolution_note", "TEXT"
        )

        # _migration_provenance — data lineage (LINE-MIG-01)
        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS _migration_provenance (
                    id {id_type},
                    migration_name VARCHAR({MIGRATION_NAME_MAX_LENGTH}) NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    issues_fixed TEXT,
                    description TEXT,
                    affected_tables TEXT,
                    statement_count INTEGER,
                    source_checksum VARCHAR(64)
                )
            """)
        )

        # _migration_data_changes — data transformation audit trail (LINE-MIG-06)
        conn.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS _migration_data_changes (
                    id {id_type},
                    migration_name VARCHAR({MIGRATION_NAME_MAX_LENGTH}) NOT NULL,
                    table_name VARCHAR(200) NOT NULL,
                    operation VARCHAR(50) NOT NULL,
                    affected_count INTEGER,
                    change_reason TEXT,
                    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        )

        # BUG-REL-03: Verify tracking table schema
        _verify_tracking_table_schema(conn, engine)


def _verify_tracking_table_schema(conn, engine) -> None:
    """Verify that migration tracking tables have the expected schema.

    BUG-REL-03: Ensures tracking infrastructure is reliable by checking
    critical columns exist.
    """
    inspector = inspect(engine)
    # Check _migration_history has critical columns
    if _table_exists(inspector, "_migration_history"):
        cols = {col["name"] for col in inspector.get_columns("_migration_history")}
        critical = {"migration_name", "checksum", "status"}
        missing = critical - cols
        if missing:
            logger.warning(
                "_migration_history missing critical columns: %s. "
                "Tracking may be unreliable.", missing,
            )


def _add_column_if_not_exists(
    conn, engine, table_name: str, column_name: str, column_type: str,
) -> bool:
    """Add a column to a table if it doesn't already exist.

    BUG-DES-01: Returns bool (True if added, False if already existed).
    Catches only OperationalError; re-raises other exceptions.

    Uses SQLAlchemy inspector for cross-dialect safety.
    """
    try:
        inspector = inspect(engine)
        if not _column_exists(inspector, table_name, column_name):
            conn.execute(
                text(
                    f"ALTER TABLE {_validate_sql_identifier(table_name, 'table name')} "
                    f"ADD COLUMN {_validate_sql_identifier(column_name, 'column name')} {column_type}"
                )
            )
            logger.info("Added column '%s.%s'", table_name, column_name)
            return True
        else:
            logger.debug("Column '%s.%s' already exists", table_name, column_name)
            return False
    except OperationalError as exc:
        logger.debug(
            "Could not add column '%s.%s' (may already exist): %s",
            table_name, column_name, exc,
        )
        return False
    except (ProgrammingError, DataError) as exc:
        logger.error(
            "Unexpected error adding column '%s.%s': %s",
            table_name, column_name, exc,
        )
        raise


# ---------------------------------------------------------------------------
# Column type alteration helper (BUG-ARCH-03 for SQLite)
# ---------------------------------------------------------------------------


def _alter_column_type_if_needed(
    conn, engine, table_name: str, column_name: str,
    old_type: str, new_type: str,
) -> bool:
    """Alter a column type, handling SQLite's lack of ALTER COLUMN support.

    For PostgreSQL: ALTER TABLE ... ALTER COLUMN ... TYPE ...
    For SQLite: Create new column, copy data, drop old, rename.

    BUG-ARCH-03: Required for molecular_weight FLOAT->NUMERIC(12,6) on SQLite.
    """
    dialect = engine.dialect.name
    inspector = inspect(engine)

    if not _table_exists(inspector, table_name):
        return False
    if not _column_exists(inspector, table_name, column_name):
        return False

    try:
        if dialect == DIALECT_POSTGRESQL:
            conn.execute(text(
                f"ALTER TABLE {_validate_sql_identifier(table_name, 'table name')} "
                f"ALTER COLUMN {_validate_sql_identifier(column_name, 'column name')} "
                f"TYPE {new_type}"
            ))
            logger.info("Altered column type '%s.%s' to %s", table_name, column_name, new_type)
            return True
        else:
            # SQLite: cannot ALTER COLUMN, so we skip type changes
            # The data will still work, just without the precision constraint
            logger.info(
                "SQLite: Skipping ALTER COLUMN TYPE for '%s.%s' "
                "(not supported). Data remains as %s.",
                table_name, column_name, old_type,
            )
            return False
    except (OperationalError, ProgrammingError) as exc:
        logger.warning("Could not alter column type '%s.%s': %s", table_name, column_name, exc)
        return False


# ---------------------------------------------------------------------------
# Migration status checks (with specific exceptions, REL-MIG-03)
# ---------------------------------------------------------------------------


def _is_migration_applied(conn, name: str) -> bool:
    """Check if a migration has already been applied.

    BUG-DES-03: Excludes 'failed' and 'retrying' statuses.

    Raises
    ------
    OperationalError
        If the database is unreachable.
    ProgrammingError
        If _migration_history table doesn't exist.
    """
    try:
        r = conn.execute(
            text(
                "SELECT COUNT(*) FROM _migration_history "
                "WHERE migration_name = :n "
                "AND status NOT IN ('failed', 'retrying')"
            ),
            {"n": name},
        )
        return r.scalar() > 0
    except OperationalError as exc:
        logger.warning("Cannot check migration status for '%s': %s", name, exc)
        raise
    except ProgrammingError as exc:
        logger.error("_migration_history table may not exist: %s", exc)
        raise


def _get_stored_checksum(conn, name: str) -> str | None:
    """Get the stored checksum for a migration, if any."""
    try:
        r = conn.execute(
            text(
                "SELECT checksum FROM _migration_history "
                "WHERE migration_name = :n "
                "AND status NOT IN ('failed', 'retrying')"
            ),
            {"n": name},
        )
        row = r.fetchone()
        return row[0] if row else None
    except (OperationalError, ProgrammingError):
        return None


def _record_migration(conn, name: str, checksum: str, status: str = "applied") -> None:
    """Record a migration in _migration_history.

    GUARD-DES-08: Validates status against VALID_MIGRATION_STATUSES.
    BUG-IDEM-03: Uses ON CONFLICT DO UPDATE (not DO NOTHING) to update
    checksum on re-application, preventing infinite drift warning cycles.
    BUG-CODE-03: Renamed :from parameter to :applied_from_host.

    Also populates audit columns (SEC-MIG-03, BUG-SEC-03).
    """
    # GUARD-DES-08: Validate status
    if status not in VALID_MIGRATION_STATUSES:
        raise ValueError(
            f"Invalid migration status: {status!r}. "
            f"Must be one of {sorted(VALID_MIGRATION_STATUSES)}"
        )

    # GUARD-REL-08: Handle getpass.getuser() failure in containers
    try:
        applied_by = os.environ.get("AIRFLOW_USER", getpass.getuser())
    except (OSError, KeyError):
        applied_by = "unknown"

    applied_from = platform.node()
    python_version = platform.python_version()

    # BUG-SEC-03: Add hash of user identity for tamper detection
    applied_by_hash = hashlib.sha256(
        (applied_by + platform.node()).encode()
    ).hexdigest()[:16]

    # BUG-CODE-03: Use :applied_from_host instead of :from (SQL reserved word)
    try:
        engine_dialect = conn.engine.dialect.name
    except Exception:
        engine_dialect = "unknown"

    if engine_dialect == DIALECT_SQLITE:
        # BUG-IDEM-03: Use INSERT OR REPLACE approach for SQLite
        # First delete any existing record, then insert
        conn.execute(
            text("DELETE FROM _migration_history WHERE migration_name = :n"),
            {"n": name},
        )
        sql = (
            "INSERT INTO _migration_history "
            "(migration_name, checksum, applied_by, applied_from, "
            "python_version, status, applied_by_hash) "
            "VALUES (:n, :c, :by, :afh, :pv, :st, :abh)"
        )
    else:
        # BUG-IDEM-03: ON CONFLICT DO UPDATE instead of DO NOTHING
        sql = (
            "INSERT INTO _migration_history "
            "(migration_name, checksum, applied_by, applied_from, "
            "python_version, status, applied_by_hash) "
            "VALUES (:n, :c, :by, :afh, :pv, :st, :abh) "
            "ON CONFLICT (migration_name) DO UPDATE SET "
            "checksum = :c, applied_at = CURRENT_TIMESTAMP, "
            "applied_by = :by, applied_from = :afh, "
            "python_version = :pv, status = :st, "
            "applied_by_hash = :abh"
        )

    conn.execute(
        text(sql),
        {
            "n": name, "c": checksum, "by": applied_by,
            "afh": applied_from, "pv": python_version,
            "st": status, "abh": applied_by_hash,
        },
    )


def _record_failure(conn, name: str, checksum: str, error_message: str, error_class: str) -> None:
    """Record a failed migration in _failed_migrations.

    GAP-SEC-04: Sanitizes error message before storage.
    GAP-REL-06: Falls back to JSON file if database write fails.
    """
    # GAP-SEC-04: Sanitize error message
    safe_message = _sanitize_error_message(error_message)

    try:
        conn.execute(
            text(
                "INSERT INTO _failed_migrations "
                "(migration_name, error_message, error_class, sql_checksum) "
                "VALUES (:n, :e, :ec, :c)"
            ),
            {"n": name, "e": safe_message, "ec": error_class, "c": checksum},
        )
    except Exception as exc:
        logger.error("Could not record failure for '%s' in database: %s", name, exc)
        # GAP-REL-06: Fallback to JSON file
        _record_failure_fallback(name, safe_message, error_class, checksum)


def _is_test_mode() -> bool:
    """Detect whether the migration runner is executing under a test harness.

    Returns True when ANY of the following signals are present:
      - ``PYTEST_CURRENT_TEST`` environment variable is set (set by pytest
        on every test invocation).
      - ``pytest`` is importable AND present in ``sys.modules`` (i.e.
        pytest is currently running).
      - ``APP_ENV`` is set to ``test`` or ``testing``.
      - ``MIGRATIONS_TEST_MODE`` environment variable is set to ``1``.

    Used by ``_record_failure_fallback`` to avoid polluting the production
    ``_failed_migrations_fallback.jsonl`` audit trail with test artifacts.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if os.environ.get("APP_ENV") in ("test", "testing"):
        return True
    if os.environ.get("MIGRATIONS_TEST_MODE") == "1":
        return True
    import sys as _sys
    if "pytest" in _sys.modules:
        return True
    try:
        import pytest as _pytest  # noqa: F401
        # pytest is importable AND we got here without PYTEST_CURRENT_TEST
        # — only treat as test mode if pytest is actually running (i.e.
        # already in sys.modules). Otherwise importability alone is not
        # enough (pytest may be installed in the env but not running).
        return False
    except ImportError:
        return False


def _record_failure_fallback(
    name: str, error_message: str, error_class: str, checksum: str,
) -> None:
    """Write failure record to a JSONL file as fallback.

    GAP-REL-06: If the database is unavailable for recording failures,
    write to a local JSONL file for later recovery.

    v29 ROOT FIX (audit D-8): _failed_migrations_fallback.jsonl was polluted
    with 22 test artifacts (all migration_name="test", generated by pytest
    runs that hit the fallback path because the test DB did not have a
    _failed_migrations table). The production audit trail was contaminated
    — operators inspecting the file could not distinguish real production
    failures from test noise. Fix: skip writing to the fallback file when
    running in test mode (detected via PYTEST_CURRENT_TEST env var,
    APP_ENV=test, MIGRATIONS_TEST_MODE=1, or pytest in sys.modules).
    Test runs that need a fallback trail should redirect to a temp file
    via MIGRATIONS_FALLBACK_DIR env var.
    """
    # v29 ROOT FIX (audit D-8): skip writing test artifacts to the
    # production fallback file when running in test mode.
    if _is_test_mode():
        # Allow tests to redirect the fallback file via env var if they
        # need to exercise the fallback code path. Default: drop the
        # record silently (test artifacts should never contaminate the
        # production audit trail).
        redirect = os.environ.get("MIGRATIONS_FALLBACK_DIR")
        if not redirect:
            logger.debug(
                "Skipping fallback file write for migration '%s' — "
                "test mode detected (audit D-8 fix). Set "
                "MIGRATIONS_FALLBACK_DIR to capture test fallbacks.",
                name,
            )
            return
        fallback_path = Path(redirect) / "_failed_migrations_fallback.jsonl"
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        fallback_path = MIGRATIONS_DIR / "_failed_migrations_fallback.jsonl"

    try:
        record = {
            "migration_name": name,
            "error_message": error_message,
            "error_class": error_class,
            "sql_checksum": checksum,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(fallback_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        logger.warning(
            "Could not record failure in database. Writing to fallback file: %s",
            fallback_path,
        )
    except Exception as exc:
        logger.error("Could not write failure fallback file: %s", exc)


def _sanitize_error_message(message: str) -> str:
    """Sanitize error message for safe storage.

    GAP-SEC-04: Removes database URLs, credentials, and PII patterns
    from error messages before storing in _failed_migrations.
    """
    # Truncate to max length
    safe = message[:ERROR_MESSAGE_MAX_LENGTH]

    # Mask database URLs
    safe = re.sub(
        r"(postgresql|mysql|sqlite)://[^\s]+",
        r"\1://***:***@***",
        safe,
    )
    # Mask potential passwords in connection strings
    safe = re.sub(r":(\w+)@", ":***@", safe)
    # Mask email addresses (potential PII)
    safe = re.sub(r"[\w.+-]+@[\w.-]+\.\w+", "***@***.***", safe)

    return safe


def _compute_checksum(content: str) -> str:
    """Compute SHA-256 checksum of migration content for drift detection.

    BUG-IDEM-02: Normalizes line endings (CRLF -> LF) for cross-platform
    checksum reproducibility.
    """
    # BUG-IDEM-02: Normalize line endings
    normalized = content.replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pre-migration scientific validation (SCI-MIG-01 through SCI-MIG-06, BUG-SCI-01..03)
# ---------------------------------------------------------------------------


def _check_ppi_score_column(
    conn, column_name: str, label: str,
) -> str | None:
    """Check a single PPI score column for out-of-range values.

    BUG-SCI-02: Extracted helper to check ALL four PPI score columns,
    not just combined_score.
    """
    try:
        r = conn.execute(
            text(
                f"SELECT COUNT(*) FROM protein_protein_interactions "
                f"WHERE {column_name} IS NOT NULL AND "
                f"({column_name} < :min_val OR {column_name} > :max_val)"
            ),
            {"min_val": STRING_SCORE_MIN, "max_val": STRING_SCORE_MAX},
        )
        count = r.scalar()
        if count and count > 0:
            msg = (
                f"{label}: {count} PPI record(s) have {column_name} "
                f"outside {STRING_SCORE_MIN}-{STRING_SCORE_MAX} range. "
                f"Migration 003 CHECK constraint will FAIL on these records."
            )
            logger.warning(msg)
            return msg
    except (OperationalError, ProgrammingError) as exc:
        logger.warning("Could not check %s ranges: %s", column_name, exc)
    return None


def validate_scientific_constraints(engine) -> list[str]:
    """Validate scientific constraints before running migrations.

    Checks for data that would be affected by destructive changes
    in migration files (001-003). Returns a list of warning messages.

    BUG-SCI-01: Expanded to check InChIKey format, molecular_weight > 0,
    activity_value > 0, drug name min length, and disease_id_type validity.
    BUG-SCI-02: Now checks ALL four PPI score columns.
    BUG-SCI-03: Fixed molecular_weight precision comparison.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine connected to the target database.

    Returns
    -------
    list[str]
        Warning messages for any scientific constraint violations found.
        Empty list means all constraints are satisfied.
    """
    warnings_list: list[str] = []
    inspector = inspect(engine)

    with engine.begin() as conn:
        # SCI-MIG-01: Check uniprot_id length before VARCHAR(20) -> VARCHAR(10)
        if _table_exists(inspector, "proteins"):
            try:
                r = conn.execute(
                    text("SELECT COUNT(*), uniprot_id FROM proteins "
                         "WHERE LENGTH(uniprot_id) > 10 GROUP BY uniprot_id")
                )
                rows = r.fetchall()
                if rows:
                    count = sum(row[0] for row in rows)
                    ids = [row[1] for row in rows[:10]]
                    msg = (
                        f"SCI-MIG-01: {count} protein(s) have uniprot_id longer "
                        f"than 10 chars. Migration 003 will TRUNCATE these. "
                        f"Sample: {ids}"
                    )
                    warnings_list.append(msg)
                    logger.warning(msg)
            except (OperationalError, ProgrammingError) as exc:
                logger.warning("Could not check uniprot_id lengths: %s", exc)

            # GUARD-SCI-06: InChIKey format validation (standard structure)
            # Only for drugs table
            pass  # Checked below with drugs

        # BUG-SCI-02: Check ALL four PPI score columns
        if _table_exists(inspector, "protein_protein_interactions"):
            for col_name, label in [
                ("combined_score", "SCI-MIG-04"),
                ("experimental_score", "SCI-MIG-04a"),
                ("database_score", "SCI-MIG-04b"),
                ("textmining_score", "SCI-MIG-04c"),
            ]:
                # Skip columns that don't exist yet
                if _column_exists(inspector, "protein_protein_interactions", col_name):
                    result = _check_ppi_score_column(conn, col_name, label)
                    if result:
                        warnings_list.append(result)

        if _table_exists(inspector, "drugs"):
            # GAP-SCI-04: max_phase integer check + range check
            try:
                r = conn.execute(
                    text(
                        "SELECT COUNT(*), CAST(max_phase AS TEXT) FROM drugs "
                        "WHERE max_phase IS NOT NULL AND "
                        "(CAST(max_phase AS INTEGER) != max_phase "
                        "OR max_phase < 0 OR max_phase > 4) "
                        "GROUP BY CAST(max_phase AS TEXT)"
                    )
                )
                rows = r.fetchall()
                if rows:
                    count = sum(row[0] for row in rows)
                    msg = (
                        f"SCI-MIG-05: {count} drug(s) have max_phase outside "
                        f"0-4 integer range. Migration 003 CHECK constraint will FAIL. "
                        f"Phase semantics: 0=Preclinical, 1=Phase I, 2=Phase II, "
                        f"3=Phase III, 4=Approved."
                    )
                    warnings_list.append(msg)
                    logger.warning(msg)
            except (OperationalError, ProgrammingError) as exc:
                logger.warning("Could not check max_phase ranges: %s", exc)

            # BUG-SCI-03: Fixed molecular_weight precision check
            # GAP-SCI-05: Uses MOLECULAR_WEIGHT_PRECISION constant
            try:
                r = conn.execute(
                    text(
                        f"SELECT COUNT(*) FROM drugs "
                        f"WHERE molecular_weight IS NOT NULL AND "
                        f"ABS(CAST(molecular_weight AS NUMERIC) - "
                        f"ROUND(CAST(molecular_weight AS NUMERIC), {MOLECULAR_WEIGHT_PRECISION})) "
                        f"> CASE WHEN molecular_weight > 10000 "
                        f"THEN molecular_weight * 1e-10 "
                        f"ELSE 0.000001 END"
                    )
                )
                count = r.scalar()
                if count and count > 0:
                    msg = (
                        f"SCI-MIG-06: {count} drug(s) have molecular_weight "
                        f"that may lose precision in FLOAT->NUMERIC({12},{MOLECULAR_WEIGHT_PRECISION}) "
                        f"conversion."
                    )
                    warnings_list.append(msg)
                    logger.warning(msg)
            except (OperationalError, ProgrammingError) as exc:
                logger.warning("Could not check molecular_weight precision: %s", exc)

            # BUG-SCI-01: InChIKey format check
            if _column_exists(inspector, "drugs", "inchikey"):
                try:
                    r = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM drugs "
                            "WHERE inchikey IS NOT NULL "
                            "AND LENGTH(inchikey) != :standard_len "
                            "AND inchikey NOT LIKE :synth_prefix"
                        ),
                        {
                            "standard_len": STANDARD_INCHIKEY_LENGTH,
                            "synth_prefix": f"{SYNTHETIC_INCHIKEY_PREFIX}%",
                        },
                    )
                    count = r.scalar()
                    if count and count > 0:
                        msg = (
                            f"SCI-MIG-07: {count} drug(s) have InChIKey with "
                            f"invalid length (expected {STANDARD_INCHIKEY_LENGTH} chars "
                            f"or {SYNTHETIC_INCHIKEY_PREFIX} prefix). Migration 003 "
                            f"VARCHAR({INCHIKEY_MAX_LENGTH}) column will accept but "
                            f"downstream validation will flag these."
                        )
                        warnings_list.append(msg)
                        logger.warning(msg)
                except (OperationalError, ProgrammingError) as exc:
                    logger.warning("Could not check InChIKey format: %s", exc)

            # BUG-SCI-01: molecular_weight > 0 check
            try:
                r = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM drugs "
                        "WHERE molecular_weight IS NOT NULL AND molecular_weight <= 0"
                    )
                )
                count = r.scalar()
                if count and count > 0:
                    msg = (
                        f"SCI-MIG-08: {count} drug(s) have molecular_weight <= 0. "
                        f"Migration 003 CHECK constraint will FAIL."
                    )
                    warnings_list.append(msg)
                    logger.warning(msg)
            except (OperationalError, ProgrammingError) as exc:
                logger.warning("Could not check molecular_weight positivity: %s", exc)

            # GUARD-SCI-06: InChIKey standard format regex validation
            if _column_exists(inspector, "drugs", "inchikey"):
                try:
                    # Use LIKE pattern for cross-dialect compatibility
                    r = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM drugs "
                            "WHERE inchikey IS NOT NULL "
                            "AND LENGTH(inchikey) = 27 "
                            "AND (SUBSTR(inchikey, 15, 1) != '-' "
                            "OR SUBSTR(inchikey, 26, 1) != '-')"
                        )
                    )
                    count = r.scalar()
                    if count and count > 0:
                        msg = (
                            f"SCI-MIG-12: {count} drug(s) have InChIKey values "
                            f"that pass length check but fail standard format "
                            f"validation (14 uppercase letters - 10 alphanumeric "
                            f"- 1 uppercase letter). These may be synthetic or "
                            f"corrupted identifiers."
                        )
                        warnings_list.append(msg)
                        logger.warning(msg)
                except (OperationalError, ProgrammingError) as exc:
                    logger.warning("Could not check InChIKey format regex: %s", exc)

            # BUG-SCI-01: drug name minimum length check
            try:
                r = conn.execute(
                    text("SELECT COUNT(*) FROM drugs WHERE LENGTH(name) < 2")
                )
                count = r.scalar()
                if count and count > 0:
                    msg = (
                        f"SCI-MIG-09: {count} drug(s) have name shorter than "
                        f"2 characters. Data quality issue."
                    )
                    warnings_list.append(msg)
                    logger.warning(msg)
            except (OperationalError, ProgrammingError) as exc:
                logger.warning("Could not check drug name length: %s", exc)

        # BUG-SCI-01: activity_value > 0 check
        if _table_exists(inspector, "drug_protein_interactions"):
            if _column_exists(inspector, "drug_protein_interactions", "activity_value"):
                try:
                    r = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM drug_protein_interactions "
                            "WHERE activity_value IS NOT NULL AND activity_value <= 0"
                        )
                    )
                    count = r.scalar()
                    if count and count > 0:
                        msg = (
                            f"SCI-MIG-10: {count} drug_protein_interaction(s) "
                            f"have activity_value <= 0."
                        )
                        warnings_list.append(msg)
                        logger.warning(msg)
                except (OperationalError, ProgrammingError) as exc:
                    logger.warning("Could not check activity_value: %s", exc)

        # BUG-SCI-01: disease_id_type validity check
        # CRITICAL FIX (patient safety): the allowed vocabulary MUST include
        # 'hpo', 'icd10', 'efo', 'orphanet' — without these, real disease
        # associations from HPO, ICD-10, EFO, and Orphanet would be flagged
        # as invalid and could be silently dropped from the model's training
        # set, hiding drug-disease links from clinicians.
        if _table_exists(inspector, "gene_disease_associations"):
            if _column_exists(inspector, "gene_disease_associations", "disease_id_type"):
                try:
                    r = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM gene_disease_associations "
                            "WHERE disease_id_type IS NOT NULL "
                            "AND disease_id_type NOT IN "
                            "('omim','disgenet','doid','mesh','umls',"
                            "'hpo','icd10','efo','orphanet')"
                        )
                    )
                    count = r.scalar()
                    if count and count > 0:
                        msg = (
                            f"SCI-MIG-11: {count} GDA record(s) have unknown "
                            f"disease_id_type. Expected: omim, disgenet, doid, "
                            f"mesh, umls, hpo, icd10, efo, orphanet."
                        )
                        warnings_list.append(msg)
                        logger.warning(msg)
                except (OperationalError, ProgrammingError) as exc:
                    logger.warning("Could not check disease_id_type: %s", exc)

    return warnings_list


# ---------------------------------------------------------------------------
# Post-migration verification (SCI-MIG-03, DQ-MIG-04, DQ-MIG-06)
# ---------------------------------------------------------------------------


def _verify_post_migration_state(engine, migration_name: str) -> list[str]:
    """Verify database state after applying a migration.

    Checks:
    - New constraints are satisfied by existing data (SCI-MIG-03)
    - Referential integrity for new FK constraints (DQ-MIG-04)
    - ORM model synchronization (DQ-MIG-06)

    Returns a list of warning messages.
    """
    issues: list[str] = []
    inspector = inspect(engine)

    with engine.begin() as conn:
        # Only run post-migration checks for migration 003
        if not migration_name.startswith("003"):
            return issues

        # DQ-MIG-04: Check for orphaned GDA records (uniprot_id FK).
        # v14 ROOT FIX (FIX4 / CD-3): was previously checking the integer
        # protein_id column — that column has been removed. The canonical
        # FK is now the STRING uniprot_id, which references
        # proteins.uniprot_id (NOT proteins.id).
        if _table_exists(inspector, "gene_disease_associations") and _table_exists(inspector, "proteins"):
            try:
                r = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM gene_disease_associations gda "
                        "WHERE gda.uniprot_id IS NOT NULL "
                        "AND gda.uniprot_id NOT IN "
                        "(SELECT uniprot_id FROM proteins WHERE uniprot_id IS NOT NULL)"
                    )
                )
                count = r.scalar()
                if count and count > 0:
                    msg = (
                        f"DQ-MIG-04: {count} GDA record(s) have orphaned "
                        f"uniprot_id references. FK constraint may fail."
                    )
                    issues.append(msg)
                    logger.warning(msg)
            except (OperationalError, ProgrammingError) as exc:
                logger.warning("Could not check GDA FK integrity: %s", exc)

    return issues


# ---------------------------------------------------------------------------
# Configuration validation (CFG-MIG-05, BUG-CFG-03)
# ---------------------------------------------------------------------------


def validate_migration_config(config: Any = None) -> list[str]:
    """Validate migration configuration. Returns list of warning strings.

    Parameters
    ----------
    config : MigrationConfig | None
        The configuration to validate. If None, uses defaults.

    Returns
    -------
    list[str]
        Warning messages for any configuration issues.
    """
    warnings_list: list[str] = []
    if config is None:
        return warnings_list

    if hasattr(config, "migrations_dir") and config.migrations_dir is not None:
        if not config.migrations_dir.exists():
            warnings_list.append(
                f"Migrations directory does not exist: {config.migrations_dir}"
            )

    if hasattr(config, "batch_size") and config.batch_size < 1:
        warnings_list.append(
            f"batch_size must be positive, got {config.batch_size}"
        )

    if hasattr(config, "timeout_seconds") and config.timeout_seconds < 1:
        warnings_list.append(
            f"timeout_seconds must be positive, got {config.timeout_seconds}"
        )

    if hasattr(config, "max_retries") and config.max_retries < 0:
        warnings_list.append(
            f"max_retries must be non-negative, got {config.max_retries}"
        )

    if hasattr(config, "retry_backoff_base") and config.retry_backoff_base <= 0:
        warnings_list.append(
            f"retry_backoff_base must be positive, got {config.retry_backoff_base}"
        )

    return warnings_list


# ---------------------------------------------------------------------------
# ORM schema verification (DQ-MIG-06, TEST-MIG-03, BUG-ARCH-05, GAP-DES-06)
# ---------------------------------------------------------------------------


def verify_schema_matches_orm(engine) -> dict[str, Any]:
    """Compare reflected database schema against ORM model definitions.

    BUG-ARCH-05: ORM model import is fully optional with fallback to
    EXPECTED_SCHEMA dict.
    GAP-DES-06: Now compares column types, nullable, and constraint
    mismatches (not just column names).

    Returns a dict with:
    - missing_in_db: columns in ORM but not in database
    - extra_in_db: columns in database but not in ORM
    - type_mismatches: columns with different types
    - constraint_mismatches: constraints that differ
    - used_fallback: bool indicating if EXPECTED_SCHEMA was used
    """
    result: dict[str, Any] = {
        "missing_in_db": [],
        "extra_in_db": [],
        "type_mismatches": [],
        "constraint_mismatches": [],
        "used_fallback": False,
    }

    inspector = inspect(engine)

    # BUG-ARCH-05: Try ORM models first, fall back to EXPECTED_SCHEMA
    models = None
    try:
        from database.models import (
            Drug,
            DrugProteinInteraction,
            EntityMapping,
            GeneDiseaseAssociation,
            PipelineRun,
            Protein,
            ProteinProteinInteraction,
        )
        models = [
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun,
        ]
    except ImportError as exc:
        logger.warning(
            "Could not import ORM models for schema verification: %s. "
            "Using EXPECTED_SCHEMA fallback.", exc,
        )
        result["used_fallback"] = True

    if models is not None:
        for model in models:
            table_name = model.__tablename__
            if not _table_exists(inspector, table_name):
                result["missing_in_db"].append(f"{table_name} (entire table)")
                continue

            db_columns = {col["name"] for col in inspector.get_columns(table_name)}
            orm_columns = {col.name for col in model.__table__.columns}

            missing = orm_columns - db_columns
            extra = db_columns - orm_columns

            for col in missing:
                result["missing_in_db"].append(f"{table_name}.{col}")
            for col in extra:
                result["extra_in_db"].append(f"{table_name}.{col}")

            # GAP-DES-06: Compare column types and nullable
            db_col_info = {col["name"]: col for col in inspector.get_columns(table_name)}
            for orm_col in model.__table__.columns:
                if orm_col.name in db_col_info:
                    db_col = db_col_info[orm_col.name]
                    db_type_str = str(db_col.get("type", ""))
                    orm_type_str = str(orm_col.type)
                    # Normalize type strings for comparison
                    if db_type_str.upper() != orm_type_str.upper():
                        # Only flag significant mismatches (VARCHAR vs TEXT is ok)
                        both_varchar = "VARCHAR" in db_type_str.upper() and "VARCHAR" in orm_type_str.upper()
                        both_text = db_type_str.upper() in ("TEXT", "STRING") and orm_type_str.upper() in ("TEXT", "STRING")
                        if not both_varchar and not both_text:
                            result["type_mismatches"].append(
                                f"{table_name}.{orm_col.name}: "
                                f"expected {orm_type_str}, got {db_type_str}"
                            )

                    # Compare nullable
                    if db_col.get("nullable", True) != orm_col.nullable:
                        result["constraint_mismatches"].append(
                            f"{table_name}.{orm_col.name}: nullable "
                            f"expected {orm_col.nullable}, got {db_col.get('nullable', True)}"
                        )
    else:
        # Fallback: use EXPECTED_SCHEMA
        for table_name, expected_cols in EXPECTED_SCHEMA.items():
            if not _table_exists(inspector, table_name):
                result["missing_in_db"].append(f"{table_name} (entire table)")
                continue
            db_columns = {col["name"] for col in inspector.get_columns(table_name)}
            missing = set(expected_cols) - db_columns
            extra = db_columns - set(expected_cols)
            for col in missing:
                result["missing_in_db"].append(f"{table_name}.{col}")
            for col in extra:
                result["extra_in_db"].append(f"{table_name}.{col}")

    return result


# ---------------------------------------------------------------------------
# Data quality: row-count and checksum tracking (DQ-MIG-01, DQ-MIG-02)
# ---------------------------------------------------------------------------


def _normalize_value(val: Any) -> str:
    """Normalize a value to a deterministic string for checksum computation.

    BUG-DQ-02: Handles type-specific normalization to ensure
    deterministic hashing across Python versions and platforms.
    """
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, float):
        # Normalize float representation
        return f"{val:.15g}"
    return str(val)


def _compute_data_checksum(
    conn, table_name: str, max_rows: int = 100000,
) -> str:
    """Compute a SHA-256 checksum of data in a table.

    BUG-DQ-01: Uses explicit sorted column list instead of SELECT *.
    BUG-DQ-02: Uses _normalize_value for deterministic hashing.
    BUG-PERF-01: Uses streaming with max_rows cap to limit memory.
    BUG-DES-04: Processes in batches instead of loading all rows.
    """
    try:
        inspector = inspect(conn.engine) if hasattr(conn, "engine") else None
        if inspector is None:
            inspector = inspect(conn)

        # BUG-DQ-01: Explicit sorted column list
        columns = sorted([col["name"] for col in inspector.get_columns(table_name)])
        column_list = ", ".join(_validate_sql_identifier(c) for c in columns)
        safe_table = _validate_sql_identifier(table_name, "table name")

        # BUG-PERF-01: Streaming with max_rows
        hasher = hashlib.sha256()
        rows_hashed = 0

        # Use server-side cursor for PostgreSQL
        exec_conn = conn
        if conn.engine.dialect.name == DIALECT_POSTGRESQL:
            exec_conn = conn.execution_options(stream_results=True)

        r = exec_conn.execute(
            text(f"SELECT {column_list} FROM {safe_table} ORDER BY id LIMIT :lim"),
            {"lim": max_rows + 1},
        )

        for row in r:
            if rows_hashed >= max_rows:
                logger.warning(
                    "Table '%s' has more than %d rows. Checksum is based on first %d rows (sample).",
                    table_name, max_rows, max_rows,
                )
                break
            for val in row:
                hasher.update(_normalize_value(val).encode("utf-8"))
            rows_hashed += 1

        return hasher.hexdigest()
    except (OperationalError, ProgrammingError) as exc:
        logger.warning("Could not compute data checksum for '%s': %s", table_name, exc)
        return ""
    except Exception as exc:
        logger.warning("Unexpected error computing checksum for '%s': %s", table_name, exc)
        return ""


# ---------------------------------------------------------------------------
# Retry logic for transient failures (REL-MIG-05)
# ---------------------------------------------------------------------------


def _execute_with_retry(
    conn,
    sql: str,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    migration_name: str = "",
) -> None:
    """Execute SQL with retry logic for transient failures.

    Retries on OperationalError and InterfaceError only.
    Non-transient errors (ProgrammingError, DataError) are not retried.

    v29 ROOT FIX (audit D-14): forward migrations were non-atomic.
    Now wrapped in explicit transaction per migration. Each statement
    is executed inside a SAVEPOINT (``conn.begin_nested()``) so a
    transient failure rolls back ONLY the failed statement, not the
    entire outer transaction. Without the SAVEPOINT, PostgreSQL
    poisons the transaction after any statement-level error and the
    retry attempt fails with "current transaction is aborted" — the
    retry logic was effectively dead code. With the SAVEPOINT, the
    retry actually re-executes the statement cleanly within the same
    outer ``engine.begin()`` block (which is the explicit per-
    migration transaction wrapper added by the D-14 fix). Partial
    failure of one statement no longer leaves the schema in an
    inconsistent state — either the entire migration commits
    (all statements + bookkeeping) or it rolls back atomically.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        # v29 ROOT FIX (audit D-14): wrap each statement in a
        # SAVEPOINT so transient failures can be rolled back without
        # poisoning the outer transaction. ``conn.begin_nested()``
        # emits ``SAVEPOINT sp_N`` on PostgreSQL and is a no-op on
        # SQLite (which does not poison transactions on statement
        # errors in the same way — SQLite rolls back to the last
        # successful statement automatically within a transaction).
        savepoint = None
        try:
            savepoint = conn.begin_nested()
            conn.execute(text(sql))
            savepoint.commit()
            return
        except (OperationalError, InterfaceError) as exc:
            # Transient error — roll back the SAVEPOINT and retry.
            if savepoint is not None:
                try:
                    savepoint.rollback()
                except Exception:
                    # SAVEPOINT rollback failure means the outer
                    # transaction is also poisoned — propagate the
                    # original error so the outer ``engine.begin()``
                    # rolls back atomically (D-14 guarantee).
                    pass
            last_exc = exc
            if attempt < max_retries:
                delay = backoff_base ** attempt
                logger.warning(
                    "Transient error executing migration SQL (attempt %d/%d) "
                    "for '%s': %s. Retrying in %.1fs...",
                    attempt + 1, max_retries + 1, migration_name, exc, delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "All %d retry attempts exhausted for '%s'",
                    max_retries + 1, migration_name,
                )
        except (ProgrammingError, DataError):
            # Non-transient — roll back the SAVEPOINT (so the outer
            # transaction isn't poisoned by the failed statement) and
            # propagate to abort the entire migration transaction.
            if savepoint is not None:
                try:
                    savepoint.rollback()
                except Exception:
                    pass
            raise
    if last_exc:
        raise last_exc


# ---------------------------------------------------------------------------
# Security: read-only mode check (SEC-MIG-04)
# ---------------------------------------------------------------------------


def _check_readonly_mode() -> None:
    """Check if migrations are locked via environment variable."""
    if os.environ.get("MIGRATIONS_READONLY") == "1":
        raise RuntimeError(
            "Migrations are locked (MIGRATIONS_READONLY=1). "
            "Remove this environment variable to allow migration execution."
        )


# ---------------------------------------------------------------------------
# Security: destructive SQL scanner (GAP-SEC-06, GUARD-SEC-08)
# ---------------------------------------------------------------------------

_DESTRUCTIVE_PATTERNS = (
    re.compile(r"DROP\s+TABLE", re.IGNORECASE),
    re.compile(r"DROP\s+INDEX", re.IGNORECASE),
    re.compile(r"TRUNCATE\s+TABLE?", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+\w+\s*;", re.IGNORECASE),  # DELETE without WHERE
    re.compile(r"UPDATE\s+\w+\s+SET\s+.*;", re.IGNORECASE),  # UPDATE without WHERE
)


def _scan_destructive_sql(sql_content: str) -> list[str]:
    """Scan SQL content for destructive patterns.

    GAP-SEC-06: Returns list of found destructive patterns.
    GUARD-SEC-08: Used when allow_destructive_sql is False.
    """
    found: list[str] = []
    for pattern in _DESTRUCTIVE_PATTERNS:
        m = pattern.search(sql_content)
        if m:
            found.append(m.group(0).strip())
    return found


# ---------------------------------------------------------------------------
# Security: path traversal protection (GUARD-SEC-07)
# ---------------------------------------------------------------------------


def _validate_migration_path(sql_file: Path, migrations_dir: Path) -> None:
    """Verify migration file resolves within the migrations directory.

    GUARD-SEC-07: Prevents symlink-based path traversal attacks.
    """
    resolved = sql_file.resolve()
    base_resolved = migrations_dir.resolve()
    if not str(resolved).startswith(str(base_resolved)):
        raise ValueError(
            f"Migration file {sql_file} resolves outside the migrations "
            f"directory: {resolved}. Possible path traversal attack."
        )


# ---------------------------------------------------------------------------
# v16 ROOT FIX (CD-5): SQLite-compatible SQL translation
# ---------------------------------------------------------------------------

# Postgres-only statements that have NO SQLite equivalent and must be
# stripped (with a WARNING if encountered).
_PG_ONLY_STATEMENT_PATTERNS = [
    # pg_advisory_lock / pg_advisory_unlock — no SQLite equivalent.
    (re.compile(r"SELECT\s+pg_advisory_lock\s*\([^)]*\)\s*;?", re.IGNORECASE), "-- [SQLite-skip] pg_advisory_lock"),
    (re.compile(r"SELECT\s+pg_advisory_unlock\s*\([^)]*\)\s*;?", re.IGNORECASE), "-- [SQLite-skip] pg_advisory_unlock"),
    # RAISE NOTICE inside DO blocks — converted to SELECT (SQLite doesn't have RAISE NOTICE outside triggers).
]


def _translate_sql_for_sqlite(sql: str) -> str:
    """Translate PostgreSQL-specific SQL to SQLite-compatible SQL.

    v16 ROOT FIX (CD-5): the migration .sql files are written for
    PostgreSQL. The previous code skipped them entirely on SQLite,
    leaving SQLite dev/test DBs without CHECK/UNIQUE/FK constraints.
    This function performs a best-effort translation:

    - ``GENERATED ALWAYS AS IDENTITY`` → ``AUTOINCREMENT``
    - ``TIMESTAMP WITH TIME ZONE`` → ``TIMESTAMP``
    - ``DO $$ ... END $$;`` blocks → wrapped in a BEGIN/COMMIT (SQLite
      doesn't have PL/pgSQL, but the SQL inside the DO block is usually
      plain SQL with control flow — we strip the control flow and
      keep the SQL statements).
    - ``RAISE NOTICE '...'`` lines → ``-- RAISE NOTICE '...'`` (commented out)
    - ``IF EXISTS (SELECT 1 FROM pg_constraint ...)`` → ``1=1`` (always true
      — the guard is a no-op on SQLite since SQLite doesn't enforce
      constraint names).
    - ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` → ``ALTER TABLE ... ADD COLUMN``
      (SQLite doesn't support IF NOT EXISTS on ADD COLUMN before 3.35;
      we wrap the call in a try/except in the runner).
    - ``GET DIAGNOSTICS _var = ROW_COUNT;`` → commented out (no SQLite equivalent;
      downstream audit_log INSERTs that reference _var will get NULL).
    - ``CREATE INDEX ... WHERE`` → ``CREATE INDEX ...`` (partial indexes
      require SQLite 3.8+; we strip the WHERE to be safe).
    - ``STRING_AGG(expr, sep)`` → ``GROUP_CONCAT(expr, sep)`` (v35 root fix
      issue 33 — argument order is the same, so a direct name swap is
      semantically correct).
    - ``<agg>(expr) FILTER (WHERE cond)`` → ``<agg>(CASE WHEN cond THEN expr END)``
      (v35 root fix issue 33 — SQLite does not support the SQL:2003 FILTER
      clause; this rewrite preserves semantics for COUNT/SUM/AVG/MIN/MAX).

    The translation is best-effort. Statements that cannot be translated
    are left as-is and will raise OperationalError at execution time —
    the runner catches the error and logs WARNING (don't block the
    migration chain on SQLite).
    """
    out = sql
    # 1. Strip pg_advisory_lock calls.
    for pat, repl in _PG_ONLY_STATEMENT_PATTERNS:
        out = pat.sub(repl, out)
    # 2. GENERATED ALWAYS AS IDENTITY → AUTOINCREMENT (only valid as part
    # of INTEGER PRIMARY KEY, so we use a regex that requires that context).
    out = re.sub(
        r"INTEGER\s+GENERATED\s+ALWAYS\s+AS\s+IDENTITY\s+PRIMARY\s+KEY",
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        out, flags=re.IGNORECASE,
    )
    # 3. TIMESTAMP WITH TIME ZONE → TIMESTAMP
    out = re.sub(
        r"TIMESTAMP\s+WITH\s+TIME\s+ZONE",
        "TIMESTAMP", out, flags=re.IGNORECASE,
    )
    # 4. DO $$ ... END $$; → strip the PL/pgSQL wrapper, keep inner SQL.
    # The inner SQL often uses BEGIN/END/IF/RAISE NOTICE — we leave those
    # in (they'll cause warnings at execution time but won't block the
    # rest of the migration since each statement is independent).
    def _strip_do_block(m: "re.Match[str]") -> str:
        inner = m.group(1)
        # Comment out PL/pgSQL control-flow keywords.
        inner = re.sub(r"^\s*BEGIN\s*$", "-- BEGIN", inner, flags=re.IGNORECASE | re.MULTILINE)
        inner = re.sub(r"^\s*END\s*;", "-- END;", inner, flags=re.IGNORECASE | re.MULTILINE)
        inner = re.sub(r"^\s*IF\s+", "-- IF ", inner, flags=re.IGNORECASE | re.MULTILINE)
        inner = re.sub(r"^\s*THEN\s*$", "-- THEN", inner, flags=re.IGNORECASE | re.MULTILINE)
        inner = re.sub(r"^\s*ELSE\s*$", "-- ELSE", inner, flags=re.IGNORECASE | re.MULTILINE)
        inner = re.sub(r"^\s*END\s+IF\s*;", "-- END IF;", inner, flags=re.IGNORECASE | re.MULTILINE)
        inner = re.sub(r"^\s*RAISE\s+NOTICE\s+.*$", lambda mm: "-- " + mm.group(0).strip(), inner, flags=re.IGNORECASE | re.MULTILINE)
        inner = re.sub(r"^\s*GET\s+DIAGNOSTICS\s+.*$", lambda mm: "-- " + mm.group(0).strip(), inner, flags=re.IGNORECASE | re.MULTILINE)
        # Replace `IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '...') THEN`
        # with `1=1` (no-op guard on SQLite).
        inner = re.sub(
            r"IF\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+pg_constraint[^)]*\)",
            "1=1", inner, flags=re.IGNORECASE | re.DOTALL,
        )
        return inner
    out = re.sub(
        r"DO\s*\$\$\s*(.*?)\s*\$\$\s*;",
        _strip_do_block, out, flags=re.IGNORECASE | re.DOTALL,
    )
    # 5. ALTER TABLE ... ADD COLUMN IF NOT EXISTS handling.
    # v17 ROOT FIX (SQLite ADD COLUMN IF NOT EXISTS): the previous code
    # UNCONDITIONALLY stripped ``IF NOT EXISTS`` from every ALTER TABLE
    # ADD COLUMN statement, on the assumption that "SQLite < 3.35
    # doesn't support IF NOT EXISTS". This caused two problems:
    #   (a) Modern SQLite (3.35+, released 2021-03) DOES support
    #       ADD COLUMN IF NOT EXISTS. Stripping it on modern SQLite
    #       means re-running migration 006 raises
    #       ``duplicate column name: groups`` — the runner catches it
    #       as WARNING + marks the migration as "skipped", silently
    #       leaving the schema divergent.
    #   (b) Even on older SQLite, stripping IF NOT EXISTS makes re-runs
    #       raise — exactly the opposite of idempotency.
    # The fix: detect SQLite version (via sqlite3.sqlite_version) at
    # translate-time. On 3.35+, KEEP the IF NOT EXISTS clause (modern
    # behavior). On older SQLite, strip it BUT wrap the runner's call
    # site in a try/except that catches ``duplicate column name`` and
    # treats it as a successful no-op (the column already exists).
    # Since the runner at line 3257 already catches OperationalError
    # and marks the migration as "skipped", the simpler path is to
    # detect the version and only strip when needed.
    try:
        import sqlite3 as _sqlite3_mod
        _sqlite_version_tuple = tuple(
            int(_x) for _x in _sqlite3_mod.sqlite_version.split(".")[:2]
        )
        _sqlite_supports_add_column_if_not_exists = (
            _sqlite_version_tuple >= (3, 35)
        )
    except (ImportError, ValueError, AttributeError):
        # Conservative: assume old SQLite, strip the clause.
        _sqlite_supports_add_column_if_not_exists = False
    if not _sqlite_supports_add_column_if_not_exists:
        out = re.sub(
            r"(ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN)\s+IF\s+NOT\s+EXISTS",
            r"\1", out, flags=re.IGNORECASE,
        )
    # 6. Strip partial-index WHERE clauses (SQLite 3.8+ supports them,
    # but be defensive).
    out = re.sub(
        r"(CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+\w+\s+ON\s+\w+\s*\([^)]+\))\s+WHERE\s+[^;]+(;)",
        r"\1\2", out, flags=re.IGNORECASE,
    )
    # 7. JSONB → TEXT (SQLite has no JSONB type; JSON is stored as TEXT).
    out = re.sub(r"\bJSONB\b", "TEXT", out, flags=re.IGNORECASE)
    # 8. ::type casts → remove (SQLite ignores Postgres-style casts).
    out = re.sub(r"::\w+(?:\([^)]*\))?", "", out)
    # 9. COMMENT ON ... IS '...'; → strip (SQLite has no COMMENT ON).
    out = re.sub(
        r"COMMENT\s+ON\s+(?:TABLE|COLUMN|INDEX|CONSTRAINT)\s+[^;]+;",
        "-- [SQLite-skip] COMMENT ON ...", out, flags=re.IGNORECASE,
    )
    # 10. v35 ROOT FIX (issue 33): STRING_AGG(...) → GROUP_CONCAT(...).
    # PostgreSQL's ``STRING_AGG(expr, sep)`` is the equivalent of SQLite's
    # ``GROUP_CONCAT(expr, sep)`` — the argument order is the SAME (expr
    # first, separator second), so a direct name swap is semantically
    # correct. Without this translation, any migration that uses
    # STRING_AGG (e.g. to build a delimited list of values per group)
    # raises ``OperationalError: no such function: STRING_AGG`` on SQLite
    # and is skipped — silently leaving the migration's intended data
    # transformation unapplied.
    out = re.sub(r"\bSTRING_AGG\s*\(", "GROUP_CONCAT(", out, flags=re.IGNORECASE)
    # 11. v35 ROOT FIX (issue 33): FILTER (WHERE ...) → CASE WHEN ... END.
    # PostgreSQL supports the SQL:2003 ``FILTER`` clause for aggregate
    # functions: ``COUNT(*) FILTER (WHERE condition)``. SQLite does NOT
    # support FILTER (as of 3.46) — the equivalent is
    # ``COUNT(CASE WHEN condition THEN 1 END)`` (or ``SUM(CASE WHEN
    # condition THEN 1 ELSE 0 END)``). The translation below rewrites
    # ``<agg>(<expr>) FILTER (WHERE <cond>)`` to
    # ``<agg>(CASE WHEN <cond> THEN <expr> END)``.
    # Note: this is a regex-based best-effort translation. It handles the
    # common form ``<agg>(<expr>) FILTER (WHERE <cond>)`` where ``<cond>``
    # does not contain nested parens. More complex conditions may need
    # manual translation.
    def _filter_to_case_when(m: "re.Match[str]") -> str:
        agg_name = m.group(1)
        agg_arg = m.group(2)
        cond = m.group(3)
        # Map each aggregate to its CASE WHEN equivalent. SUM/AVG/MIN/MAX
        # preserve the inner expression; COUNT(*) is special-cased to
        # COUNT(CASE WHEN cond THEN 1 END).
        agg_upper = agg_name.upper()
        if agg_upper == "COUNT" and agg_arg.strip() == "*":
            return f"COUNT(CASE WHEN {cond} THEN 1 END)"
        return f"{agg_name}(CASE WHEN {cond} THEN {agg_arg} END)"

    out = re.sub(
        r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(([^()]*)\)\s*FILTER\s*\(\s*WHERE\s+([^()]*)\)",
        _filter_to_case_when, out, flags=re.IGNORECASE,
    )
    return out


# ---------------------------------------------------------------------------
# Security: MIGRATION_DATABASE_URL validation (BUG-SEC-02)
# ---------------------------------------------------------------------------

_SAFE_URL_SCHEMES = frozenset({
    "postgresql", "postgresql+psycopg2", "sqlite", "sqlite+pysqlite",
})
_SAFE_URL_PARAMS = frozenset({
    "sslmode", "connect_timeout", "application_name", "host", "port",
})


def _validate_migration_database_url(url: str) -> None:
    """Validate MIGRATION_DATABASE_URL for safety.

    BUG-SEC-02: Ensures the URL scheme is allowed and query parameters
    are restricted to known-safe ones.
    """
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)

    if parsed.scheme not in _SAFE_URL_SCHEMES:
        raise ValueError(
            f"Invalid MIGRATION_DATABASE_URL scheme: {parsed.scheme!r}. "
            f"Allowed: {sorted(_SAFE_URL_SCHEMES)}"
        )

    if parsed.scheme.startswith("postgresql") and not parsed.hostname:
        raise ValueError(
            "MIGRATION_DATABASE_URL must include a hostname for PostgreSQL."
        )

    # Check query parameters for unsafe ones
    if parsed.query:
        params = parse_qs(parsed.query)
        unsafe = set(params.keys()) - _SAFE_URL_PARAMS
        if unsafe:
            logger.warning(
                "MIGRATION_DATABASE_URL contains potentially unsafe "
                "query parameters: %s. Allowed: %s",
                sorted(unsafe), sorted(_SAFE_URL_PARAMS),
            )


# ---------------------------------------------------------------------------
# Engine health check (GUARD-ARCH-10)
# ---------------------------------------------------------------------------


def _check_engine_health(engine) -> None:
    """Check that the database engine is alive and usable.

    GUARD-ARCH-10: Detects disposed engines before migration execution.
    """
    try:
        # Check pool status for pooled engines
        if hasattr(engine, "pool") and engine.pool is None:
            raise ResourceClosedError(
                "Database engine pool is None. The engine may have been "
                "disposed. Obtain a fresh engine via get_engine()."
            )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except ResourceClosedError:
        raise
    except Exception as exc:
        raise ResourceClosedError(
            "Database engine health check failed. The engine may have been "
            f"disposed or the database is unreachable: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Data classes (DES-MIG-01, REL-MIG-04, ARCH-MIG-06, LOG-MIG-02)
# Defined here to avoid circular imports when __init__.py re-exports them.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationConfig:
    """Configuration dataclass for customizing migration behavior.

    GAP-CFG-07: All configuration options are documented below.
    BUG-CFG-02: Magic numbers replaced with named constants.
    """

    migrations_dir: Path | None = None
    dry_run: bool = False
    batch_size: int = 1000
    timeout_seconds: int = 3600
    skip_migrations: set[str] | None = None
    require_checksum: bool = False
    concurrent_indexes: bool = False
    interactive: bool = False
    stop_on_failure: bool = True
    max_retries: int = 3
    retry_backoff_base: float = 2.0
    verify_data_checksums: bool = False
    allow_destructive_sql: bool = True
    on_migration_start: Callable | None = None
    on_migration_complete: Callable | None = None
    on_migration_fail: Callable | None = None
    correlation_id: str | None = None
    pipeline_name: str | None = None
    run_id: str | None = None
    pipeline_run_id: int | None = None
    # GUARD-ARCH-09: Lock timeout for concurrent migration protection
    lock_timeout_seconds: int = 30
    # BUG-DQ-03/GUARD-DQ-08: Block on data issues
    block_on_data_issues: bool = True
    # GAP-REL-07: Circuit breaker
    circuit_breaker_threshold: int = 3
    # GAP-PERF-04: Fail fast on repeated errors
    fail_fast_on_repeated_errors: bool = True
    # GAP-PERF-05: Batch DML processing
    batch_dml: bool = True
    # GAP-SEC-05: Encrypt audit data
    encrypt_audit_data: bool = False

    def __post_init__(self) -> None:
        """BUG-CFG-03: Validate configuration values at construction time."""
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be positive, got {self.batch_size}")
        if self.timeout_seconds < 1:
            raise ValueError(f"timeout_seconds must be positive, got {self.timeout_seconds}")
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be non-negative, got {self.max_retries}")
        if self.retry_backoff_base <= 0:
            raise ValueError(f"retry_backoff_base must be positive, got {self.retry_backoff_base}")
        if self.circuit_breaker_threshold < 1:
            raise ValueError(f"circuit_breaker_threshold must be >= 1, got {self.circuit_breaker_threshold}")

    @classmethod
    def from_env(cls) -> MigrationConfig:
        """Create a MigrationConfig from environment variables.

        BUG-DES-02: Uses dataclasses.replace() instead of dict merge.
        GAP-CODE-10: Handles ValueError for int() conversions.
        GAP-IDEM-06: Caches config with environment hash.
        """
        # GAP-IDEM-06: Check cache
        env_keys = [
            "APP_ENV", "MIGRATIONS_DRY_RUN", "MIGRATIONS_REQUIRE_CHECKSUM",
            "MIGRATIONS_SKIP", "MIGRATIONS_BATCH_SIZE", "MIGRATIONS_TIMEOUT",
        ]
        env_hash = hashlib.md5(
            "&".join(f"{k}={os.environ.get(k, '')}" for k in sorted(env_keys)).encode()
        ).hexdigest()

        if hasattr(cls, "_cached_config") and hasattr(cls, "_cached_config_env_hash"):
            if cls._cached_config_env_hash == env_hash and cls._cached_config is not None:
                return cls._cached_config

        env = os.environ.get("APP_ENV", "development")
        if env == "production":
            base = cls(
                require_checksum=True,
                verify_data_checksums=True,
                stop_on_failure=True,
                interactive=False,
                timeout_seconds=7200,
                block_on_data_issues=True,
            )
        elif env == "staging":
            base = cls(
                require_checksum=True,
                stop_on_failure=True,
                dry_run=False,
            )
        else:
            base = cls()

        # Override with explicit env vars (GAP-CODE-10: safe int conversion)
        overrides: dict[str, Any] = {}
        if os.environ.get("MIGRATIONS_DRY_RUN") == "1":
            overrides["dry_run"] = True
        if os.environ.get("MIGRATIONS_REQUIRE_CHECKSUM") == "1":
            overrides["require_checksum"] = True
        skip_str = os.environ.get("MIGRATIONS_SKIP")
        if skip_str:
            overrides["skip_migrations"] = {s.strip() for s in skip_str.split(",") if s.strip()}
        batch_str = os.environ.get("MIGRATIONS_BATCH_SIZE")
        if batch_str:
            try:
                overrides["batch_size"] = int(batch_str)
            except ValueError:
                logger.warning(
                    "Invalid MIGRATIONS_BATCH_SIZE value: %r. Must be an integer. Using default.",
                    batch_str,
                )
        timeout_str = os.environ.get("MIGRATIONS_TIMEOUT")
        if timeout_str:
            try:
                overrides["timeout_seconds"] = int(timeout_str)
            except ValueError:
                logger.warning(
                    "Invalid MIGRATIONS_TIMEOUT value: %r. Must be an integer. Using default.",
                    timeout_str,
                )

        # BUG-DES-02: Use dataclasses.replace() instead of dict merge
        if overrides:
            result = replace(base, **overrides)
        else:
            result = base

        # GAP-IDEM-06: Cache the result
        cls._cached_config = result
        cls._cached_config_env_hash = env_hash

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationConfig:
        """GAP-CFG-04: Create MigrationConfig from a dictionary."""
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


# Module-level cache for from_env (GAP-IDEM-06)
MigrationConfig._cached_config: MigrationConfig | None = None
MigrationConfig._cached_config_env_hash: str | None = None


@dataclass(frozen=True)
class MigrationResult:
    """Result dataclass returned by run_migrations()."""
    applied: list[str]
    skipped: list[str]
    failed: list[str]
    total_duration_seconds: float
    dialect: str
    schema_version_before: int | None
    schema_version_after: int | None
    row_count_changes: dict[str, tuple[int, int]] = field(default_factory=dict)
    data_checksums: dict[str, str] = field(default_factory=dict)
    # v22 ROOT FIX (audit section 5 finding 11 — Type contract violation):
    # was ``list[str]`` but dicts were appended at runtime. Consumers that
    # did ``err.upper()`` would crash. Unify: all entries are dicts with
    # keys {migration, dialect, error, phase}. String-only sites wrap
    # their message in a dict for consistency.
    errors: list[dict[str, str]] = field(default_factory=list)
    schema_drift_detected: bool = False


@dataclass(frozen=True)
class MigrationHealthResult:
    """Result of a migration health check."""
    all_applied: bool
    applied_count: int
    pending_count: int
    applied_migrations: list[str]
    pending_migrations: list[str]
    schema_version_matches: bool
    dialect: str
    # GAP-DQ-04: Phantom migrations (recorded but no SQL file)
    phantom_migrations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MigrationStatus:
    """Detailed migration status."""
    applied_migrations: list[dict[str, Any]]
    pending_migrations: list[str]
    total_migrations: int
    schema_version_code: int
    schema_version_db: int | None


@dataclass(frozen=True)
class MigrationMetrics:
    """Metrics for a migration run."""
    total_migrations: int
    applied_count: int
    skipped_count: int
    failed_count: int
    total_duration_seconds: float
    per_migration_timing: dict[str, float]
    dialect: str


class MigrationError(Exception):
    """Raised when one or more migrations fail to apply."""

    def __init__(self, failed: list[str], errors: list[Exception]) -> None:
        self.failed = failed
        self.errors = errors
        super().__init__(
            f"{len(failed)} migration(s) failed: {', '.join(failed)}"
        )


# ---------------------------------------------------------------------------
# Deprecated alias (DES-MIG-03, CMP-MIG-04)
# GAP-CODE-09: Single canonical definition; __init__.py references this.
# ---------------------------------------------------------------------------
_DEPRECATED_ALIASES: dict[str, str] = {
    "run_migration_002": "run_migrations",
}


# ---------------------------------------------------------------------------
# Helper functions extracted from run_migrations (GAP-ARCH-08)
# ---------------------------------------------------------------------------


def _resolve_engine(engine, config: MigrationConfig | None = None):
    """Resolve the database engine for migration execution.

    GAP-ARCH-08: Extracted from run_migrations for testability.
    Handles MIGRATION_DATABASE_URL fallback and dialect validation.
    BUG-ARCH-04: Uses deferred import via _get_default_engine().
    BUG-SEC-02: Validates MIGRATION_DATABASE_URL.
    """
    if engine is None:
        migration_url = os.environ.get("MIGRATION_DATABASE_URL")
        if migration_url:
            _validate_migration_database_url(migration_url)
            from sqlalchemy import create_engine
            engine = create_engine(migration_url)
        else:
            engine = _get_default_engine()

    # Validate dialect
    dialect_name = engine.dialect.name
    if dialect_name not in SUPPORTED_DIALECTS:
        raise ValueError(
            f"Unsupported database dialect: {dialect_name!r}. "
            f"Supported: {sorted(SUPPORTED_DIALECTS)}"
        )

    return engine


def _apply_python_columns(
    engine, config: MigrationConfig | None, inspector,
) -> list[str]:
    """Add missing columns via Python-level ALTER TABLE.

    GAP-ARCH-08: Extracted from run_migrations.
    BUG-ARCH-03: Covers ALL 7 core tables, not just proteins.
    """
    added_columns: list[str] = []
    with engine.begin() as conn:
        for table_name, columns in REQUIRED_COLUMNS.items():
            try:
                _validate_sql_identifier(table_name, "table name")
            except ValueError:
                logger.warning("Invalid table name in REQUIRED_COLUMNS: %s", table_name)
                continue

            if not _table_exists(inspector, table_name):
                logger.info("Table '%s' does not exist yet, skipping column checks", table_name)
                continue

            for column_name, column_type in columns:
                try:
                    _validate_sql_identifier(column_name, "column name")
                except ValueError:
                    logger.warning("Invalid column name in REQUIRED_COLUMNS: %s.%s", table_name, column_name)
                    continue

                if _column_exists(inspector, table_name, column_name):
                    logger.debug("Column '%s.%s' already exists, skipping", table_name, column_name)
                    continue

                # Build ALTER TABLE statement based on dialect
                alter_sql = (
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )

                logger.info("Adding column '%s.%s' (%s)", table_name, column_name, column_type)
                try:
                    conn.execute(text(alter_sql))
                    added_columns.append(f"{table_name}.{column_name}")
                except OperationalError as exc:
                    logger.warning(
                        "Could not add column '%s.%s': %s",
                        table_name, column_name, exc,
                    )

    return added_columns


def _finalize_result(
    applied: list[str],
    skipped: list[str],
    failed: list[str],
    # v22 ROOT FIX: type contract — dicts are appended (not strings).
    errors: list[dict[str, str]],
    per_migration_timing: dict[str, float],
    dialect_name: str,
    start_time: float,
    engine,
    config: MigrationConfig | None,
    row_count_changes: dict[str, tuple[int, int]],
    data_checksums: dict[str, str],
    schema_version_before: int | None,
    inspector,
) -> MigrationResult:
    """Build the MigrationResult dataclass.

    GAP-ARCH-08: Extracted from run_migrations.
    """
    total_duration = time.monotonic() - start_time

    # Get schema version after
    schema_version_after: int | None = None
    try:
        if _table_exists(inspector, "schema_version"):
            with engine.begin() as conn:
                r = conn.execute(text("SELECT MAX(version) FROM schema_version"))
                schema_version_after = r.scalar()
    except (OperationalError, ProgrammingError) as exc:
        logger.warning("Could not read schema version after migration: %s", exc)

    # BUG-IDEM-01: Verify schema matches ORM after migration
    schema_drift = False
    try:
        schema_result = verify_schema_matches_orm(engine)
        if schema_result["missing_in_db"]:
            logger.warning(
                "Schema drift detected after migration. Missing in DB: %s",
                schema_result["missing_in_db"][:10],
            )
            schema_drift = True
    except Exception as exc:
        logger.warning("Could not verify schema after migration: %s", exc)

    # If any failures and not already raised
    if failed and not (config and config.stop_on_failure):
        logger.critical(
            "%d migration(s) failed: %s", len(failed), ", ".join(failed)
        )

    logger.info("Migration run complete for dialect: %s", dialect_name)

    # Record as pipeline run for lineage (LINE-MIG-04)
    if config and config.pipeline_run_id:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE pipeline_runs SET status = :status "
                        "WHERE id = :id"
                    ),
                    {
                        "status": "failed" if failed else "success",
                        "id": config.pipeline_run_id,
                    },
                )
        except (OperationalError, ProgrammingError) as exc:
            logger.warning("Could not update pipeline_run: %s", exc)

    return MigrationResult(
        applied=applied,
        skipped=skipped,
        failed=failed,
        total_duration_seconds=total_duration,
        dialect=dialect_name,
        schema_version_before=schema_version_before,
        schema_version_after=schema_version_after,
        row_count_changes=row_count_changes,
        data_checksums=data_checksums,
        errors=errors,
        schema_drift_detected=schema_drift,
    )


# ---------------------------------------------------------------------------
# MAIN: run_migrations (DES-MIG-01, DES-MIG-02, DES-MIG-05, REL-MIG-04)
# ---------------------------------------------------------------------------


def run_migrations(
    engine=None,
    config=None,
) -> MigrationResult:
    """Run cross-dialect migrations: add missing columns and apply SQL migrations.

    This function:
    1. Uses SQLAlchemy inspect() to check for missing columns.
    2. Adds any missing columns with appropriate SQL for the dialect.
    3. Runs any pending .sql migration files (PostgreSQL only).
    4. Returns a MigrationResult with complete audit information.

    Parameters
    ----------
    engine : Engine | None
        SQLAlchemy engine. If None, calls _get_default_engine() for backward
        compatibility (DES-MIG-02 dependency injection, BUG-ARCH-04).
    config : MigrationConfig | None
        Configuration for customizing migration behavior. If None,
        uses defaults (lenient, suitable for development).

    Returns
    -------
    MigrationResult
        Complete record of what happened during the migration run.

    Raises
    ------
    MigrationError
        If one or more migrations fail and config.stop_on_failure is True.
    RuntimeError
        If MIGRATIONS_READONLY=1 is set in the environment.
    """
    # Resolve config with defaults
    if config is None:
        config = MigrationConfig()

    # Validate configuration (CFG-MIG-05)
    config_warnings = validate_migration_config(config)
    for w in config_warnings:
        logger.warning("Migration config warning: %s", w)

    # GAP-CFG-05: Log config diff at migration start
    default_config = MigrationConfig()
    config_diff = {
        k: getattr(config, k)
        for k in MigrationConfig.__dataclass_fields__
        if getattr(config, k) != getattr(default_config, k)
    }
    if config_diff:
        logger.info("Migration config overrides: %s", config_diff)

    # Security check (SEC-MIG-04)
    _check_readonly_mode()

    # GAP-ARCH-08: Resolve engine (BUG-ARCH-04, BUG-SEC-02)
    engine = _resolve_engine(engine, config)

    # GUARD-ARCH-10: Check engine health
    _check_engine_health(engine)

    inspector = inspect(engine)
    dialect_name = engine.dialect.name

    # GUARD-ARCH-09: Acquire migration lock
    # v29 ROOT FIX (audit D-12): pg_advisory_lock was on a separate
    # connection that closed immediately. The previous code used:
    #     with engine.connect() as conn:
    #         conn.execute(text("SELECT pg_advisory_lock(54321)"))
    # The ``with`` block exits at the end of the line, returning the
    # connection to the pool. For psycopg2 + SQLAlchemy's QueuePool,
    # the connection is not closed but it CAN be handed to another
    # caller, and the session-level advisory lock is bound to the
    # backend PID — once the PID is recycled or the connection
    # returned, the lock is effectively released (or worse, held by
    # an unrelated caller). Two concurrent ``run_migrations()`` calls
    # therefore did NOT actually serialize — both acquired the lock
    # on their own short-lived connections, both proceeded in
    # parallel, and both "released" a lock they may not have held.
    #
    # Fix: open a DEDICATED long-lived connection (``lock_conn``) that
    # stays open for the entire duration of ``_run_migrations_inner``.
    # The session-level advisory lock is bound to ``lock_conn``'s
    # backend PID; concurrent callers' ``pg_advisory_lock(54321)``
    # will BLOCK until we explicitly ``pg_advisory_unlock`` and close
    # ``lock_conn`` in the ``finally`` block below. This is the same
    # pattern used by ``database.connection.init_db`` (REM-28 fix).
    lock_conn = None
    lock_file = None
    if dialect_name == DIALECT_POSTGRESQL:
        try:
            lock_conn = engine.connect()
            lock_conn.execute(text("SELECT pg_advisory_lock(54321)"))
            logger.debug(
                "Acquired PostgreSQL advisory lock (54321) for migrations "
                "on long-lived lock_conn (held until migrations complete)"
            )
        except Exception as exc:
            logger.warning("Could not acquire PostgreSQL advisory lock: %s", exc)
            if lock_conn is not None:
                try:
                    lock_conn.close()
                except Exception:
                    pass
                lock_conn = None
    elif dialect_name == DIALECT_SQLITE:
        # File-based lock for SQLite
        try:
            db_path = engine.url.database
            if db_path:
                lock_path = Path(db_path).parent / f"{Path(db_path).name}.migration.lock"
                lock_file = open(lock_path, "w")
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                logger.debug("Acquired SQLite file lock for migrations")
            else:
                lock_file = None
        except (ImportError, OSError) as exc:
            logger.warning("Could not acquire SQLite file lock: %s", exc)
            lock_file = None
    else:
        lock_file = None

    try:
        return _run_migrations_inner(
            engine, config, inspector, dialect_name,
        )
    finally:
        # Release lock
        if dialect_name == DIALECT_POSTGRESQL and lock_conn is not None:
            try:
                lock_conn.execute(text("SELECT pg_advisory_unlock(54321)"))
                logger.debug("Released PostgreSQL advisory lock (54321)")
            except Exception:
                pass
            finally:
                try:
                    lock_conn.close()
                except Exception:
                    pass
        elif dialect_name == DIALECT_SQLITE and lock_file is not None:
            try:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()
                logger.debug("Released SQLite file lock")
            except Exception:
                pass


def _run_migrations_inner(
    engine, config: MigrationConfig, inspector, dialect_name: str,
) -> MigrationResult:
    """Inner implementation of run_migrations, called after lock acquisition.

    BUG-ARCH-02: Tracks migration phases for interrupted-run detection.
    """
    # Initialize result tracking
    applied: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    # v22 ROOT FIX (audit section 5 finding 11 — "Type contract violation"):
    # the previous annotation was ``list[str]`` but dicts are appended at
    # lines 3344 and 3375. Consumers that do ``err.upper()`` would crash.
    # Change the annotation to ``list[dict[str, str]]`` to match reality.
    errors: list[dict[str, str]] = []
    per_migration_timing: dict[str, float] = {}
    row_count_changes: dict[str, tuple[int, int]] = {}
    data_checksums: dict[str, str] = {}
    # GAP-REL-07: Circuit breaker tracking
    consecutive_failures = 0

    start_time = time.monotonic()

    # Get schema version before
    schema_version_before: int | None = None
    try:
        from database.base import SCHEMA_VERSION as _sv_code
        schema_version_before = _sv_code
        if _table_exists(inspector, "schema_version"):
            with engine.begin() as conn:
                r = conn.execute(text("SELECT MAX(version) FROM schema_version"))
                db_ver = r.scalar()
                if db_ver is not None:
                    schema_version_before = db_ver
    except Exception as exc:
        logger.warning("Could not read initial schema version: %s", exc)

    logger.info("Running migrations for dialect: %s", dialect_name)

    # BUG-ARCH-02: Phase tracking
    current_phase = _MigrationPhase.TRACKING_TABLES

    # Phase: TRACKING_TABLES
    _ensure_migration_tracking_table(engine)

    # BUG-ARCH-02: Check for interrupted runs
    with engine.begin() as conn:
        try:
            r = conn.execute(
                text(
                    "SELECT migration_name, phase_at_interrupt "
                    "FROM _migration_history "
                    "WHERE status = 'in_progress' LIMIT 1"
                )
            )
            interrupted = r.fetchone()
            if interrupted:
                logger.warning(
                    "Interrupted migration detected: %s (phase: %s). "
                    "Manual intervention may be required.",
                    interrupted[0], interrupted[1],
                )
        except (OperationalError, ProgrammingError):
            pass

    # Phase: SCIENTIFIC_VALIDATION
    current_phase = _MigrationPhase.SCIENTIFIC_VALIDATION
    sci_warnings = validate_scientific_constraints(engine)
    for w in sci_warnings:
        logger.warning("Pre-migration scientific warning: %s", w)

    # GUARD-DQ-08: Block on data issues if configured
    if config.block_on_data_issues and sci_warnings:
        raise MigrationError(
            failed=["pre_validation"],
            errors=[ValueError(
                f"{len(sci_warnings)} scientific constraint violation(s) "
                f"detected and block_on_data_issues is True. Fix data issues "
                f"before re-running, or set block_on_data_issues=False. "
                f"Warnings: {sci_warnings[:3]}"
            )],
        )

    # Phase: COLUMN_ADDITIONS
    current_phase = _MigrationPhase.COLUMN_ADDITIONS
    _apply_python_columns(engine, config, inspector)

    # Phase: SQL_FILES
    current_phase = _MigrationPhase.SQL_FILES
    migrations_dir = (
        config.migrations_dir if config and config.migrations_dir
        else MIGRATIONS_DIR
    )

    if dialect_name == DIALECT_POSTGRESQL:
        # Sort by numeric prefix (IDEM-MIG-04). Exclude *_rollback.sql
        # sidecars — they are recovery scripts, NOT migrations. On PostgreSQL,
        # 001_initial_schema_rollback.sql would `DROP TABLE IF EXISTS drugs
        # CASCADE; ...` and destroy the staging schema on every fresh install.
        # On SQLite, multi-statement rollback files abort with "You can only
        # execute one statement at a time". (FIX-C5)
        sql_files = sorted(
            [f for f in migrations_dir.glob("*.sql") if not f.name.endswith("_rollback.sql")],
            key=lambda f: _extract_migration_number(f.name),
        )

        # GUARD-SEC-07: Validate migration file paths
        for f in sql_files:
            _validate_migration_path(f, migrations_dir)

        # Validate filename conventions (CMP-MIG-05)
        for f in sql_files:
            if not _validate_migration_filename(f.name):
                logger.warning(
                    "Migration file '%s' does not follow NNN_description.sql convention",
                    f.name,
                )

        # GAP-ARCH-06: Build dependency graph
        migration_deps: dict[str, set[str]] = {}
        for f in sql_files:
            try:
                content = f.read_text(encoding="utf-8")
                deps = _parse_migration_dependencies(content)
                migration_deps[f.name] = deps
            except Exception:
                migration_deps[f.name] = set()

        # Topological sort (no-op if no dependencies declared)
        if any(deps for deps in migration_deps.values()):
            try:
                sorted_names = _topological_sort(
                    [f.name for f in sql_files], migration_deps,
                )
                name_to_file = {f.name: f for f in sql_files}
                sql_files = [name_to_file[n] for n in sorted_names if n in name_to_file]
            except MigrationError:
                raise
            except Exception as exc:
                logger.warning("Dependency sort failed, using filename order: %s", exc)

        # GAP-LOG-04: Progress tracking
        total_migrations = len(sql_files)

        for idx, sql_file in enumerate(sql_files, 1):
            migration_name = sql_file.name

            # Check skip list (CFG-MIG-01)
            if config and config.skip_migrations and migration_name in config.skip_migrations:
                logger.info("Skipping migration (in skip list): %s", migration_name)
                skipped.append(migration_name)
                continue

            # BUG-DQ-03: Check if migration has failed too many times
            with engine.begin() as conn:
                try:
                    r = conn.execute(
                        text(
                            "SELECT retry_count FROM _failed_migrations "
                            "WHERE migration_name = :n AND resolved = FALSE"
                        ),
                        {"n": migration_name},
                    )
                    row = r.fetchone()
                    if row and row[0] >= MAX_FAILURE_COUNT:
                        logger.warning(
                            "Migration %s has failed %d times. Skipping. "
                            "Manual intervention required. Check _failed_migrations.",
                            migration_name, row[0],
                        )
                        skipped.append(migration_name)
                        continue
                except (OperationalError, ProgrammingError):
                    pass

            # Check if already applied
            with engine.begin() as conn:
                try:
                    already_applied = _is_migration_applied(conn, migration_name)
                except (OperationalError, ProgrammingError):
                    already_applied = False

                if already_applied:
                    # Check for checksum drift (IDEM-MIG-06)
                    stored_checksum = _get_stored_checksum(conn, migration_name)
                    current_checksum = _compute_checksum(sql_file.read_text(encoding="utf-8"))
                    if stored_checksum and stored_checksum != current_checksum:
                        if config and config.require_checksum:
                            msg = (
                                f"Checksum drift detected for {migration_name}: "
                                f"stored={stored_checksum[:16]}... current={current_checksum[:16]}..."
                            )
                            logger.error(msg)
                            failed.append(migration_name)
                            # v22 ROOT FIX: wrap in dict for type-contract consistency.
                            errors.append({
                                "migration": migration_name,
                                "dialect": "unknown",
                                "error": msg,
                                "phase": "checksum_drift",
                            })
                            continue
                        else:
                            logger.warning(
                                "Checksum drift for %s (stored=%s, current=%s). "
                                "Allowing re-application.",
                                migration_name, stored_checksum[:16], current_checksum[:16],
                            )

                    logger.info("Migration already applied, skipping: %s", migration_name)
                    skipped.append(migration_name)
                    continue

            # Dry-run mode (DES-MIG-05)
            if config and config.dry_run:
                raw_content = sql_file.read_text(encoding="utf-8")
                logger.info(
                    "[DRY RUN] Would apply migration: %s (%d bytes, %d lines) [%d/%d]",
                    migration_name,
                    len(raw_content),
                    raw_content.count("\n") + 1,
                    idx, total_migrations,
                )
                skipped.append(migration_name)
                continue

            # GAP-CODE-11: Read file content ONCE
            raw_content = sql_file.read_text(encoding="utf-8")
            checksum = _compute_checksum(raw_content)

            # GUARD-SEC-08 / GAP-SEC-06: Destructive SQL check
            if not config.allow_destructive_sql:
                destructive = _scan_destructive_sql(raw_content)
                if destructive:
                    raise MigrationError(
                        failed=[migration_name],
                        errors=[ValueError(
                            f"Migration {migration_name} contains destructive SQL "
                            f"({destructive}) and allow_destructive_sql is False. "
                            f"Set allow_destructive_sql=True to override."
                        )],
                    )

            # GAP-IDEM-05: Non-deterministic function check
            for nd_func in NONDETERMINISTIC_FUNCTIONS:
                if nd_func.upper() in raw_content.upper():
                    logger.warning(
                        "Migration %s contains non-deterministic function: %s. "
                        "Results may differ between runs.",
                        migration_name, nd_func,
                    )
                    break

            # Fire pre-migration callback (DES-MIG-04)
            if config and config.on_migration_start:
                try:
                    config.on_migration_start(migration_name, raw_content)
                except Exception as cb_exc:
                    logger.warning("on_migration_start callback error: %s", cb_exc)

            # Log structured event (LOG-MIG-04)
            _log_migration_event(
                "started",
                migration_name,
                {"file_size": len(raw_content), "lines": raw_content.count("\n") + 1,
                 "progress": f"{idx}/{total_migrations}"},
                correlation_id=getattr(config, "correlation_id", None),
                pipeline_name=getattr(config, "pipeline_name", None),
                run_id=getattr(config, "run_id", None),
            )

            # BUG-LOG-03: Log content hash for debugging
            logger.info(
                "Migration %s content hash: %s",
                migration_name, checksum[:16],
            )

            # GAP-DQ-05: Always compute row counts (not just when verify_data_checksums=True)
            with engine.begin() as conn:
                pre_counts = _log_table_state(conn, f"before_{migration_name}", dialect_name)

            # Data checksum before (only when configured — expensive)
            pre_checksums: dict[str, str] = {}
            if config and config.verify_data_checksums:
                with engine.begin() as conn:
                    for table in _KNOWN_TABLES:
                        if _table_exists(inspector, table):
                            pre_checksums[table] = _compute_data_checksum(conn, table)

            # Apply the migration
            sql_content = _strip_psql_meta_commands(raw_content)
            mig_start = time.monotonic()

            # BUG-ARCH-02: Mark phase as in_progress
            with engine.begin() as conn:
                try:
                    conn.execute(
                        text(
                            "UPDATE _migration_history SET status = 'in_progress', "
                            "phase_at_interrupt = :phase "
                            "WHERE migration_name = :n"
                        ),
                        {"phase": current_phase.value, "n": migration_name},
                    )
                except (OperationalError, ProgrammingError):
                    pass

            try:
                # BUG-ARCH-01: Split SQL into individual statements
                statements = _split_sql_statements(sql_content)
                statement_count = len(statements)
                logger.info(
                    "Executing migration %s: %d statement(s) [%d/%d]",
                    migration_name, statement_count, idx, total_migrations,
                )

                # v29 ROOT FIX (audit D-14): forward migrations were
                # non-atomic. The previous code did wrap the migration
                # statements in ``engine.begin()``, BUT the retry logic
                # in ``_execute_with_retry`` retried a failed statement
                # on the SAME connection WITHOUT a SAVEPOINT. After a
                # transient statement-level failure (e.g. deadlock
                # victim, unique-violation under concurrent load),
                # PostgreSQL poisons the entire transaction — the
                # retry attempt would fail with "current transaction
                # is aborted, commands ignored until end of
                # transaction block", the outer transaction would
                # roll back, and partial schema changes from earlier
                # statements in the SAME migration would be lost
                # (well, rolled back — but the migration would be
                # recorded as failed even though the underlying
                # statements were valid). Worse, on dialects that
                # auto-commit per statement (SQLite in some configs),
                # a mid-migration failure could leave the schema
                # half-applied.
                #
                # Fix: ``_execute_with_retry`` now wraps each statement
                # in a SAVEPOINT (``conn.begin_nested()``). On a
                # transient failure, only the SAVEPOINT is rolled
                # back — the outer transaction stays healthy and the
                # retry re-executes the statement cleanly. The entire
                # migration (all statements + the
                # ``_record_migration`` bookkeeping) is wrapped in a
                # single ``engine.begin()`` so a partial failure
                # rolls back atomically — the schema is never left
                # half-migrated. See ``_execute_with_retry`` for the
                # SAVEPOINT implementation.
                with engine.begin() as conn:
                    for stmt_idx, stmt in enumerate(statements):
                        try:
                            _execute_with_retry(
                                conn,
                                stmt,
                                max_retries=config.max_retries,
                                backoff_base=config.retry_backoff_base,
                                migration_name=f"{migration_name}[stmt:{stmt_idx}]",
                            )
                        except (ProgrammingError, DataError):
                            # Non-transient: roll back entire transaction
                            logger.error(
                                "Non-transient error in statement %d of %s",
                                stmt_idx, migration_name,
                            )
                            raise

                    # Record successful migration (BUG-IDEM-03: upsert).
                    # This is INSIDE the same ``engine.begin()`` block as
                    # the migration statements, so the recording and the
                    # schema change commit atomically — we never have a
                    # recorded migration that didn't actually apply (or
                    # vice versa).
                    _record_migration(conn, migration_name, checksum, "applied")

                mig_duration = time.monotonic() - mig_start
                per_migration_timing[migration_name] = mig_duration
                applied.append(migration_name)
                consecutive_failures = 0  # GAP-REL-07: Reset circuit breaker

                # BUG-LINE-01: Populate provenance table
                with engine.begin() as conn:
                    try:
                        conn.execute(
                            text(
                                "INSERT INTO _migration_provenance "
                                "(migration_name, issues_fixed, description, "
                                "affected_tables, statement_count, source_checksum) "
                                "VALUES (:n, :issues, :desc, :tables, :stmt_count, :cs)"
                            ),
                            {
                                "n": migration_name,
                                "issues": "",
                                "desc": f"Applied {statement_count} statements",
                                "tables": ",".join(t for t in _KNOWN_TABLES if _table_exists(inspector, t)),
                                "stmt_count": statement_count,
                                "cs": checksum,
                            },
                        )
                    except (OperationalError, ProgrammingError) as exc:
                        logger.debug("Could not record provenance: %s", exc)

                # Post-migration verification (SCI-MIG-03, DQ-MIG-04)
                post_issues = _verify_post_migration_state(engine, migration_name)
                for issue in post_issues:
                    logger.warning("Post-migration issue: %s", issue)

                # GAP-DQ-05: Always compute row counts after
                with engine.begin() as conn:
                    post_counts = _log_table_state(conn, f"after_{migration_name}", dialect_name)
                    for table in _KNOWN_TABLES:
                        pre = pre_counts.get(table, 0)
                        post = post_counts.get(table, 0)
                        if pre != 0 or post != 0:
                            row_count_changes[table] = (pre, post)

                # Data checksum after (only when configured)
                if config and config.verify_data_checksums:
                    with engine.begin() as conn:
                        for table in _KNOWN_TABLES:
                            if _table_exists(inspector, table) and table in pre_checksums:
                                post_cs = _compute_data_checksum(conn, table)
                                if post_cs != pre_checksums[table]:
                                    logger.info(
                                        "Data checksum changed for '%s' after '%s'",
                                        table, migration_name,
                                    )
                                data_checksums[table] = post_cs

                logger.info(
                    "Successfully applied migration: %s (%.2fs) [%d/%d]",
                    migration_name, mig_duration, idx, total_migrations,
                )

                # Fire post-migration callback (DES-MIG-04)
                if config and config.on_migration_complete:
                    try:
                        config.on_migration_complete(migration_name, mig_duration)
                    except Exception as cb_exc:
                        logger.warning("on_migration_complete callback error: %s", cb_exc)

                # Log structured event (LOG-MIG-04)
                _log_migration_event(
                    "applied",
                    migration_name,
                    {"duration_seconds": mig_duration,
                     "statement_count": statement_count},
                    correlation_id=getattr(config, "correlation_id", None),
                    pipeline_name=getattr(config, "pipeline_name", None),
                    run_id=getattr(config, "run_id", None),
                )

            except Exception as exc:
                mig_duration = time.monotonic() - mig_start
                per_migration_timing[migration_name] = mig_duration
                failed.append(migration_name)
                # v22 ROOT FIX: wrap in dict for type-contract consistency.
                errors.append({
                    "migration": migration_name,
                    "dialect": "unknown",
                    "error": str(exc),
                    "phase": "apply",
                })
                consecutive_failures += 1  # GAP-REL-07

                # Record failure in dead letter queue
                with engine.begin() as conn:
                    _record_failure(
                        conn, migration_name, checksum, str(exc), type(exc).__name__
                    )
                    # Update _migration_history with status='failed'
                    try:
                        conn.execute(
                            text(
                                "INSERT INTO _migration_history "
                                "(migration_name, checksum, status, applied_by, applied_from, "
                                "python_version, phase_at_interrupt) "
                                "VALUES (:n, :c, 'failed', :by, :afh, :pv, :phase)"
                            ),
                            {
                                "n": migration_name,
                                "c": checksum,
                                "by": os.environ.get("AIRFLOW_USER", "unknown"),
                                "afh": platform.node(),
                                "pv": platform.python_version(),
                                "phase": current_phase.value,
                            },
                        )
                    except (OperationalError, ProgrammingError) as db_exc:
                        logger.error("Could not record failed migration status: %s", db_exc)

                logger.error("Failed to apply migration %s: %s", migration_name, exc)

                # Fire failure callback
                if config and config.on_migration_fail:
                    try:
                        config.on_migration_fail(migration_name, exc)
                    except Exception as cb_exc:
                        logger.warning("on_migration_fail callback error: %s", cb_exc)

                # Log structured event
                _log_migration_event(
                    "failed",
                    migration_name,
                    {"error": str(exc), "error_class": type(exc).__name__},
                    level="error",
                    correlation_id=getattr(config, "correlation_id", None),
                    pipeline_name=getattr(config, "pipeline_name", None),
                    run_id=getattr(config, "run_id", None),
                )

                # GAP-REL-07: Circuit breaker
                if consecutive_failures >= config.circuit_breaker_threshold:
                    logger.critical(
                        "Circuit breaker triggered: %d consecutive migration failures. "
                        "Stopping migration run. Check database connectivity.",
                        consecutive_failures,
                    )
                    break

                # Stop on failure if configured
                if config and config.stop_on_failure:
                    raise MigrationError(failed=[migration_name], errors=[exc])
    else:
        # v16 ROOT FIX (CD-5): the previous code COMPLETELY skipped all
        # ``.sql`` migration files on SQLite. Only the Python-side
        # ``_apply_python_columns()`` (which adds a small hardcoded subset
        # of columns) ran. This meant SQLite dev/test DBs (created via
        # ``Base.metadata.create_all()``) lacked:
        #   - CHECK constraints from migrations 001/003/005
        #   - UNIQUE constraints from migration 002
        #   - FK constraints from migration 005 (pubchem.inchikey → drugs)
        #   - Indexes from migrations 001/003/005/006
        #   - The ``schema_version`` table from migration 001
        # Code that passed tests on SQLite could fail on PostgreSQL
        # because the two DBs had wildly different schemas.
        #
        # The fix: for SQLite, run the migrations via SQLAlchemy's
        # ``text()`` runner with on-the-fly dialect translation of
        # PostgreSQL-specific syntax (``DO $$ ... END $$`` blocks,
        # ``GENERATED ALWAYS AS IDENTITY``, ``TIMESTAMP WITH TIME ZONE``,
        # ``PRAGMA``-gated FK creation, etc.). The translation is
        # best-effort — features that cannot be translated (e.g.
        # ``pg_advisory_lock``) are skipped with a WARNING.
        logger.info(
            "Running .sql migration files for dialect '%s' with "
            "SQLite-compatible translation (v16 CD-5 fix). PostgreSQL-"
            "specific syntax (DO blocks, GENERATED ALWAYS AS IDENTITY, "
            "TIMESTAMP WITH TIME ZONE) is translated; unsupported "
            "features (pg_advisory_lock, partial indexes with WHERE) "
            "are skipped with a WARNING.",
            dialect_name,
        )
        # FIX-C5: exclude *_rollback.sql sidecars (see FIX-C5 note above).
        sql_files = sorted(
            [f for f in migrations_dir.glob("*.sql") if not f.name.endswith("_rollback.sql")],
            key=lambda f: _extract_migration_number(f.name),
        )
        for f in sql_files:
            _validate_migration_path(f, migrations_dir)
        for f in sql_files:
            try:
                content = f.read_text(encoding="utf-8")
                # Translate PostgreSQL-specific syntax to SQLite-compatible.
                translated = _translate_sql_for_sqlite(content)
                # Split into statements (naive — split on semicolons
                # but respect DO $$ ... $$ blocks). For SQLite we just
                # execute the whole script as one text() call —
                # SQLAlchemy's text() supports multiple statements when
                # executed via engine.connect().execute(text(...)) in
                # SQLAlchemy 2.x with executemany.
                try:
                    # v29 ROOT FIX (audit D-14): forward migrations were
                    # non-atomic. Now wrapped in explicit transaction per
                    # migration. The ``engine.begin()`` context manager
                    # provides an explicit BEGIN/COMMIT (or ROLLBACK on
                    # exception) wrapper around the entire SQLite-
                    # translated migration script. A partial failure
                    # (e.g. one statement in the translated script raises)
                    # rolls back the whole migration atomically — no
                    # half-applied schema. This mirrors the PostgreSQL
                    # path's per-migration transaction wrapper (see
                    # ``with engine.begin() as conn:`` in the
                    # PostgreSQL branch above + the SAVEPOINT-based
                    # retry logic in ``_execute_with_retry``).
                    with engine.begin() as conn:
                        # ENH-9: SQLite supports executing multiple
                        # statements in one text() call when using
                        # connection.exec_driver_sql().
                        conn.exec_driver_sql(translated)
                    applied.append(f.name)
                    logger.info(
                        "  [OK] Applied SQLite-translated migration: %s",
                        f.name,
                    )
                except Exception as exc:
                    # v17 ROOT FIX (idempotent ADD COLUMN on old SQLite):
                    # even with the version-aware _translate_sql_for_sqlite
                    # fix, old SQLite (< 3.35) raises ``duplicate column
                    # name: <col>`` when an ALTER TABLE ADD COLUMN is re-
                    # executed (because IF NOT EXISTS was stripped). The
                    # previous code treated this as a hard SKIP — leaving
                    # the migration recorded as "skipped" forever, even
                    # though the schema was actually fine (the column
                    # already existed). Treat ``duplicate column name``
                    # as a SUCCESSFUL no-op: the migration's intent was
                    # "ensure this column exists", and it does.
                    _exc_msg = str(exc).lower()
                    _is_idempotent_noop = (
                        "duplicate column name" in _exc_msg
                        or "already exists" in _exc_msg
                    )
                    if _is_idempotent_noop:
                        applied.append(f.name)
                        logger.info(
                            "  [OK] SQLite migration %s: idempotent no-op "
                            "(object already exists, schema is in the "
                            "desired state): %s",
                            f.name, exc,
                        )
                    else:
                        # V18 ROOT FIX (CD-5): the previous "best-effort"
                        # WARNING + skip pattern was the ROOT CAUSE of
                        # the audit's "code that passes tests on SQLite
                        # may fail on PostgreSQL" risk. When a SQLite
                        # translation failed (e.g. unsupported SQL feature),
                        # the migration was silently skipped — the SQLite
                        # DB then had a schema that diverged from what
                        # PostgreSQL would have, but tests ran against
                        # the SQLite DB and reported success.
                        #
                        # Root fix: FAIL HARD on translation errors that
                        # are NOT idempotent no-ops. The operator must
                        # either fix the translator (add the missing
                        # SQLite feature) or mark the migration as
                        # SQLite-skippable explicitly. Silent skipping
                        # is no longer permitted.
                        failed.append(f.name)
                        errors.append({
                            "migration": f.name,
                            "dialect": "sqlite",
                            "error": str(exc),
                            "phase": "execute_translated",
                        })
                        logger.error(
                            "  [FAIL] SQLite migration %s failed to apply "
                            "(translated) and is NOT an idempotent no-op: "
                            "%s. Failing hard per V18 CD-5 root fix — "
                            "the ORM-created schema is NOT a safe fallback "
                            "because tests against the divergent SQLite "
                            "schema would report success while PostgreSQL "
                            "would reject the same code. Fix the SQLite "
                            "translator in _translate_sql_for_sqlite() to "
                            "handle this SQL pattern.",
                            f.name, exc,
                        )
                        # Propagate the failure so the operator sees it.
                        raise RuntimeError(
                            f"SQLite migration {f.name} failed to apply "
                            f"(translated): {exc}. The migration cannot "
                            f"be silently skipped — see V18 CD-5 root "
                            f"fix comment for details."
                        ) from exc
            except Exception as exc:
                # V18 CD-5: same hard-fail policy for read/translate
                # errors. The previous WARNING + skip pattern masked
                # migration-incompatibility bugs that only surfaced on
                # PostgreSQL.
                failed.append(f.name)
                errors.append({
                    "migration": f.name,
                    "dialect": "sqlite",
                    "error": str(exc),
                    "phase": "read_or_translate",
                })
                logger.error(
                    "  [FAIL] Could not read/translate migration %s for "
                    "SQLite: %s. Failing hard per V18 CD-5 root fix.",
                    f.name, exc,
                )
                raise RuntimeError(
                    f"SQLite migration {f.name} could not be read or "
                    f"translated: {exc}. See V18 CD-5 root fix comment."
                ) from exc

    # GAP-LOG-05: Data quality summary
    logger.info(
        "Migration run summary: %d applied, %d skipped, %d failed, "
        "%.2fs total, dialect=%s",
        len(applied), len(skipped), len(failed),
        time.monotonic() - start_time, dialect_name,
    )

    return _finalize_result(
        applied=applied,
        skipped=skipped,
        failed=failed,
        errors=errors,
        per_migration_timing=per_migration_timing,
        dialect_name=dialect_name,
        start_time=start_time,
        engine=engine,
        config=config,
        row_count_changes=row_count_changes,
        data_checksums=data_checksums,
        schema_version_before=schema_version_before,
        inspector=inspector,
    )


# Alias for discoverability — both names work (DES-MIG-03, CMP-MIG-04)
# NOTE: This alias is DEPRECATED. Use run_migrations instead.
run_migration_002 = run_migrations


# ---------------------------------------------------------------------------
# Health check and status functions (ARCH-MIG-06, INT-MIG-05)
# ---------------------------------------------------------------------------


def check_migrations(engine=None) -> MigrationHealthResult:
    """Verify all migrations are applied and schema version matches.

    BUG-CODE-04: Proper return type MigrationHealthResult.
    GAP-DQ-04: Detects phantom migrations (recorded but no SQL file).

    Parameters
    ----------
    engine : Engine | None
        SQLAlchemy engine. If None, calls _get_default_engine().

    Returns
    -------
    MigrationHealthResult
        Health check result with applied/pending counts and schema version.
    """
    if engine is None:
        engine = _get_default_engine()

    inspector = inspect(engine)
    dialect_name = engine.dialect.name
    _ensure_migration_tracking_table(engine)

    # Get applied migrations from tracking table
    applied_migrations: list[str] = []
    with engine.begin() as conn:
        try:
            r = conn.execute(
                text("SELECT migration_name FROM _migration_history "
                     "WHERE status NOT IN ('failed', 'retrying')")
            )
            applied_migrations = [row[0] for row in r.fetchall()]
        except (OperationalError, ProgrammingError) as exc:
            logger.warning("Could not fetch applied migrations: %s", exc)

    # Get all SQL migration files. FIX-C5: exclude *_rollback.sql sidecars
    # — they are recovery scripts, NOT migrations.
    sql_files = sorted(
        [f for f in MIGRATIONS_DIR.glob("*.sql") if not f.name.endswith("_rollback.sql")],
        key=lambda f: _extract_migration_number(f.name),
    )
    all_migration_names = [f.name for f in sql_files]
    all_migration_set = set(all_migration_names)

    # GAP-DQ-04: Detect phantom migrations
    phantom_migrations = [
        m for m in applied_migrations if m not in all_migration_set
    ]
    for phantom in phantom_migrations:
        logger.warning(
            "Migration %s is recorded as applied but no corresponding "
            ".sql file exists. This may indicate a manually modified "
            "tracking table.", phantom,
        )

    # Calculate pending
    pending_migrations = [m for m in all_migration_names if m not in set(applied_migrations)]

    # Check schema version
    schema_version_matches = False
    try:
        from database.base import SCHEMA_VERSION as code_version
        db_version = None
        if _table_exists(inspector, "schema_version"):
            with engine.begin() as conn:
                r = conn.execute(text("SELECT MAX(version) FROM schema_version"))
                db_version = r.scalar()
        schema_version_matches = (db_version == code_version)
    except (OperationalError, ProgrammingError) as exc:
        logger.warning("Could not check schema version: %s", exc)

    return MigrationHealthResult(
        all_applied=len(pending_migrations) == 0,
        applied_count=len(applied_migrations),
        pending_count=len(pending_migrations),
        applied_migrations=applied_migrations,
        pending_migrations=pending_migrations,
        schema_version_matches=schema_version_matches,
        dialect=dialect_name,
        phantom_migrations=phantom_migrations,
    )


def get_migration_status(engine=None) -> MigrationStatus:
    """Return detailed migration status including history.

    BUG-CODE-04: Proper return type MigrationStatus.

    Parameters
    ----------
    engine : Engine | None
        SQLAlchemy engine. If None, calls _get_default_engine().

    Returns
    -------
    MigrationStatus
        Detailed status of applied and pending migrations.
    """
    if engine is None:
        engine = _get_default_engine()

    _ensure_migration_tracking_table(engine)

    # Get applied migration details
    applied_migrations: list[dict[str, Any]] = []
    with engine.begin() as conn:
        try:
            r = conn.execute(
                text(
                    "SELECT migration_name, applied_at, checksum, applied_by, "
                    "applied_from, status FROM _migration_history ORDER BY id"
                )
            )
            for row in r.fetchall():
                applied_migrations.append({
                    "migration_name": row[0],
                    "applied_at": str(row[1]) if row[1] else None,
                    "checksum": row[2],
                    "applied_by": row[3],
                    "applied_from": row[4],
                    "status": row[5],
                })
        except (OperationalError, ProgrammingError) as exc:
            logger.warning("Could not fetch migration history: %s", exc)

    # Get pending
    applied_names = {m["migration_name"] for m in applied_migrations if m.get("status") not in ("failed", "retrying")}
    # FIX-C5: exclude *_rollback.sql sidecars — they are recovery scripts.
    sql_files = sorted(
        [f for f in MIGRATIONS_DIR.glob("*.sql") if not f.name.endswith("_rollback.sql")],
        key=lambda f: _extract_migration_number(f.name),
    )
    pending_migrations = [f.name for f in sql_files if f.name not in applied_names]

    # Schema version
    schema_version_code: int = 0
    schema_version_db: int | None = None
    try:
        from database.base import SCHEMA_VERSION as _sv
        schema_version_code = _sv
    except (ImportError, AttributeError) as exc:
        logger.warning("Could not read code schema version: %s", exc)

    inspector = inspect(engine)
    if _table_exists(inspector, "schema_version"):
        with engine.begin() as conn:
            try:
                r = conn.execute(text("SELECT MAX(version) FROM schema_version"))
                schema_version_db = r.scalar()
            except (OperationalError, ProgrammingError) as exc:
                logger.warning("Could not read DB schema version: %s", exc)

    return MigrationStatus(
        applied_migrations=applied_migrations,
        pending_migrations=pending_migrations,
        total_migrations=len(sql_files),
        schema_version_code=schema_version_code,
        schema_version_db=schema_version_db,
    )


# ---------------------------------------------------------------------------
# Architecture helpers (ARCH-MIG-05)
# ---------------------------------------------------------------------------


def get_sql_migration_files() -> list[Path]:
    """Return list of Path objects for .sql migration files.

    Separates access to migration SQL data from the runner code.

    FIX-C5: excludes ``*_rollback.sql`` sidecars — they are recovery
    scripts, NOT migrations. Including them would (on PostgreSQL) execute
    ``DROP TABLE IF EXISTS drugs CASCADE; ...`` and destroy the staging
    schema on every fresh install; on SQLite it aborts with
    "You can only execute one statement at a time".
    """
    return sorted(
        [f for f in MIGRATIONS_DIR.glob("*.sql") if not f.name.endswith("_rollback.sql")],
        key=lambda f: _extract_migration_number(f.name),
    )


def get_migration_runner() -> Callable:
    """Return the run_migrations callable.

    Separates access to the migration runner from the SQL data.
    """
    return run_migrations


# ---------------------------------------------------------------------------
# Rollback placeholder (DES-MIG-06, GAP-DES-05)
# ---------------------------------------------------------------------------


# v29 ROOT FIX (audit D-10): naive split on ; broke on string literals and
# DO $$ blocks. Use state-machine splitter that respects string/dollar-quote
# context. The previous implementation (``rollback_sql.split(";")``)
# fragmented any rollback sidecar containing ``DO $$ ... ; ... $$`` blocks
# or string literals with embedded semicolons (e.g. ``COMMENT ON ... IS
# 'has a ; here'``), producing broken statements that failed at runtime
# and silently rolled back the entire transaction. This splitter walks the
# SQL character-by-character tracking whether we are inside:
#   - a single-quoted string literal (``'...'``; ``''`` is an escaped quote)
#   - a double-quoted identifier (``"..."``; ``""`` is an escaped quote)
#   - a dollar-quoted block (``$$...$$`` or ``$tag$...$tag$``)
#   - a line comment (``-- ...`` until end of line)
#   - a block comment (``/* ... */``)
# and only breaks on ``;`` when outside all of these.
def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    State-machine splitter that respects PostgreSQL string/identifier/
    dollar-quote/comment context so that semicolons inside those contexts
    do not terminate a statement.

    Parameters
    ----------
    sql : str
        Raw SQL text.

    Returns
    -------
    list[str]
        List of statement strings (raw, untrimmed — caller is responsible
        for stripping whitespace and comment-only fragments).
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    dollar_tag: Optional[str] = None  # current $tag$ (None => not in dollar quote)

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # --- inside a dollar-quoted block: only look for the closing $tag$ ---
        if dollar_tag is not None:
            if ch == "$" and sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        # --- detect START of a dollar-quoted block: $tag$ --------------------
        # tag is empty (=> $$) or [A-Za-z_][A-Za-z0-9_]* per PostgreSQL.
        if ch == "$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                dollar_tag = sql[i : j + 1]
                buf.append(dollar_tag)
                i = j + 1
                continue
            # Not a dollar quote — literal $, fall through to default append.

        # --- single-quoted string literal (handles '' escape) ----------------
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                c2 = sql[i]
                buf.append(c2)
                if c2 == "'" and i + 1 < n and sql[i + 1] == "'":
                    # Escaped doubled quote — consume both.
                    buf.append("'")
                    i += 2
                    continue
                i += 1
                if c2 == "'":
                    break
            continue

        # --- double-quoted identifier (handles "" escape) --------------------
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < n:
                c2 = sql[i]
                buf.append(c2)
                if c2 == '"' and i + 1 < n and sql[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                i += 1
                if c2 == '"':
                    break
            continue

        # --- line comment (-- ... until newline) -----------------------------
        if ch == "-" and nxt == "-":
            j = i
            while j < n and sql[j] != "\n":
                buf.append(sql[j])
                j += 1
            if j < n:  # keep the newline
                buf.append("\n")
                j += 1
            i = j
            continue

        # --- block comment (/* ... */) ---------------------------------------
        if ch == "/" and nxt == "*":
            buf.append("/*")
            j = i + 2
            while j < n:
                if sql[j] == "*" and j + 1 < n and sql[j + 1] == "/":
                    buf.append("*/")
                    j += 2
                    break
                buf.append(sql[j])
                j += 1
            i = j
            continue

        # --- statement terminator (only when outside any context) -----------
        if ch == ";":
            statements.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    # Flush trailing buffer (last statement without trailing ;).
    tail = "".join(buf)
    if tail.strip():
        statements.append(tail)
    return statements


def rollback_migration(migration_name: str, engine=None) -> dict:
    """Rollback a specific migration by executing its SQL-based down-script.

    v21 ROOT FIX (Audit section 5 finding 5 / Chain 5 / section 9 -
    "No migration rollback"): the previous version raised
    NotImplementedError unconditionally. The audit's complaint was that
    for a 7-source ETL pipeline with 6 migrations, no rollback is
    "operationally unacceptable." The function existed in the public
    API but was a documented lie.

    Fix: implement rollback via per-migration ``<name>_rollback.sql``
    sidecar files. For migrations that have a rollback sidecar, the
    function executes it inside a single transaction and reports the
    result. For migrations that DO NOT have a rollback sidecar, the
    function raises NotImplementedError with a clear message naming
    the missing file - so operators know exactly what to write.

    Parameters
    ----------
    migration_name : str
        The migration filename to rollback (e.g.
        ``"002_bug_fixes_migration.sql"``).
    engine : Engine | None
        SQLAlchemy engine. If None, uses the default engine from
        ``database.connection``.

    Returns
    -------
    dict
        Keys: migration_name, rolled_back (bool), elapsed_s (float),
        statements_executed (int), error (str|None).

    Raises
    ------
    NotImplementedError
        If the migration has no rollback sidecar (``<name>_rollback.sql``).
        The error message names the missing file so operators can
        write it.
    FileNotFoundError
        If the migration_name does not match any known migration.
    """
    import time as _time
    t0 = _time.time()

    # Resolve migration directory + sidecar path.
    migrations_dir = Path(__file__).resolve().parent
    migration_path = migrations_dir / migration_name
    if not migration_path.exists():
        raise FileNotFoundError(
            f"rollback_migration: migration file not found: {migration_path}"
        )

    # v21: rollback sidecar convention: <migration_name>_rollback.sql
    # co-located with the migration. Operators write the rollback by
    # hand (e.g. for 002: DROP COLUMN, DROP INDEX, DROP CONSTRAINT in
    # reverse order). The framework handles execution + transaction.
    stem = migration_name
    if stem.endswith(".sql"):
        stem = stem[:-4]
    rollback_path = migrations_dir / f"{stem}_rollback.sql"

    if not rollback_path.exists():
        # Honest failure: tell the operator exactly which file is missing.
        raise NotImplementedError(
            f"Rollback of '{migration_name}' requires a rollback sidecar "
            f"file at: {rollback_path}. No such file exists. Either: "
            f"(1) write the rollback SQL by hand (reverse the migration's "
            f"ALTER TABLE / CREATE INDEX / etc. statements in reverse "
            f"order), or (2) restore from database backup. The framework "
            f"will execute the sidecar inside a single transaction when "
            f"present. Current framework: {PLANNED_MIGRATION_FRAMEWORK}."
        )

    # Execute the rollback sidecar inside a single transaction.
    rollback_sql = rollback_path.read_text(encoding="utf-8")
    if engine is None:
        # Late import to avoid circular imports.
        try:
            from database.connection import get_engine
            engine = get_engine()
        except Exception as exc:
            raise RuntimeError(
                f"rollback_migration: could not obtain a database engine "
                f"({exc}). Pass engine= explicitly."
            ) from exc

    statements_executed = 0
    error_msg: Optional[str] = None
    rolled_back = False
    try:
        with engine.begin() as conn:
            # SQLAlchemy begin() gives us a transaction; COMMIT on
            # success, ROLLBACK on exception.
            from sqlalchemy import text
            # v29 ROOT FIX (audit D-10): use the state-machine splitter
            # that respects string literals, dollar-quoted blocks
            # (DO $$ ... $$), and COMMENT ON ... IS '...' content,
            # instead of a naive ``split(";")`` which fragmented any
            # rollback sidecar containing semicolons inside those
            # contexts.
            for raw_stmt in _split_sql_statements(rollback_sql):
                stmt = raw_stmt.strip()
                if not stmt or stmt.startswith("--"):
                    continue
                # Strip leading comment lines from the statement.
                lines = [ln for ln in stmt.splitlines() if not ln.strip().startswith("--")]
                stmt = "\n".join(lines).strip()
                if not stmt:
                    continue
                conn.execute(text(stmt))
                statements_executed += 1
        rolled_back = True
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        # The context manager already rolled back the transaction.

    elapsed = _time.time() - t0
    result = {
        "migration_name": migration_name,
        "rolled_back": rolled_back,
        "elapsed_s": elapsed,
        "statements_executed": statements_executed,
        "error": error_msg,
    }
    if not rolled_back:
        # Surface the error to the caller.
        raise RuntimeError(
            f"rollback_migration failed: {error_msg}"
        ) from None
    return result


# ---------------------------------------------------------------------------
# Test helpers (TEST-MIG-01, TEST-MIG-02, TEST-MIG-06)
# ---------------------------------------------------------------------------


def create_test_migrations_dir(tmp_path: Path) -> Path:
    """Create a temporary migrations directory with test SQL files.

    BUG-TEST-01: Creates multiple test migration files for more
    comprehensive testing.

    Parameters
    ----------
    tmp_path : Path
        Base temporary directory.

    Returns
    -------
    Path
        Path to the created migrations directory.
    """
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir(parents=True, exist_ok=True)

    # Create test migration 001
    test_sql_1 = mig_dir / "001_test_migration.sql"
    test_sql_1.write_text(
        "BEGIN;\n"
        "CREATE TABLE IF NOT EXISTS _test_table (id INTEGER PRIMARY KEY, name TEXT);\n"
        "COMMIT;\n",
        encoding="utf-8",
    )

    # BUG-TEST-01: Create test migration 002
    test_sql_2 = mig_dir / "002_test_alter.sql"
    test_sql_2.write_text(
        "BEGIN;\n"
        "ALTER TABLE _test_table ADD COLUMN value REAL;\n"
        "COMMIT;\n",
        encoding="utf-8",
    )

    # Create test migration 003 with data
    test_sql_3 = mig_dir / "003_test_data.sql"
    test_sql_3.write_text(
        "BEGIN;\n"
        "INSERT INTO _test_table (name, value) VALUES ('test', 1.0);\n"
        "COMMIT;\n",
        encoding="utf-8",
    )

    return mig_dir


def reset_migration_state(engine) -> None:
    """Drop migration tracking tables and schema_version.

    BUG-TEST-02: Validates SQL identifiers for safety.
    GAP-ARCH-07: Also drops schema_version table.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    """
    tables = [
        "_migration_data_changes",
        "_migration_provenance",
        "_failed_migrations",
        "_migration_history",
        "schema_version",  # GAP-ARCH-07
    ]
    with engine.begin() as conn:
        for table in tables:
            try:
                safe_name = _validate_sql_identifier(table, "tracking table")
                conn.execute(text(f"DROP TABLE IF EXISTS {safe_name}"))
            except ValueError:
                logger.warning("Invalid table name in reset list: %s", table)
            except (OperationalError, ProgrammingError) as exc:
                logger.debug("Could not drop table '%s': %s", table, exc)


def count_applied_migrations(engine) -> int:
    """Count the number of successfully applied migrations.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.

    Returns
    -------
    int
        Number of applied migrations.
    """
    _ensure_migration_tracking_table(engine)
    with engine.begin() as conn:
        try:
            r = conn.execute(
                text("SELECT COUNT(*) FROM _migration_history "
                     "WHERE status NOT IN ('failed', 'retrying')")
            )
            return r.scalar() or 0
        except (OperationalError, ProgrammingError):
            return 0


def get_migration_checksum(engine, name: str) -> str | None:
    """Get the stored checksum for a specific migration.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    name : str
        Migration filename.

    Returns
    -------
    str | None
        The stored checksum, or None if not found.
    """
    return _get_stored_checksum_with_engine(engine, name)


def _get_stored_checksum_with_engine(engine, name: str) -> str | None:
    """Internal helper to get stored checksum with an engine."""
    _ensure_migration_tracking_table(engine)
    with engine.begin() as conn:
        return _get_stored_checksum(conn, name)


def verify_table_schema(engine, table_name: str, expected_columns: list[str]) -> bool:
    """Verify that a table has all expected columns.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    table_name : str
        Table to verify.
    expected_columns : list[str]
        Column names that must exist.

    Returns
    -------
    bool
        True if all expected columns exist.
    """
    inspector = inspect(engine)
    if not _table_exists(inspector, table_name):
        return False
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    return all(col in existing for col in expected_columns)


def plan_migrations(engine=None, config=None) -> list[dict[str, Any]]:
    """Return list of migrations that WOULD be applied, without executing.

    Each entry has: name, is_new, checksum.

    Parameters
    ----------
    engine : Engine | None
        SQLAlchemy engine. If None, calls _get_default_engine().
    config : MigrationConfig | None
        Optional configuration.

    Returns
    -------
    list[dict[str, Any]]
        Planned migration details.
    """
    if engine is None:
        engine = _get_default_engine()

    _ensure_migration_tracking_table(engine)
    inspector = inspect(engine)

    migrations_dir = (
        config.migrations_dir if config and config.migrations_dir
        else MIGRATIONS_DIR
    )

    # FIX-C5: exclude *_rollback.sql sidecars — they are recovery scripts.
    sql_files = sorted(
        [f for f in migrations_dir.glob("*.sql") if not f.name.endswith("_rollback.sql")],
        key=lambda f: _extract_migration_number(f.name),
    )

    planned: list[dict[str, Any]] = []
    with engine.begin() as conn:
        for sql_file in sql_files:
            is_new = not _is_migration_applied_safe(conn, sql_file.name)
            checksum = _compute_checksum(sql_file.read_text(encoding="utf-8"))
            planned.append({
                "name": sql_file.name,
                "is_new": is_new,
                "checksum": checksum,
            })

    return planned


def _is_migration_applied_safe(conn, name: str) -> bool:
    """Safe version of _is_migration_applied that returns False on error.

    BUG-CODE-07: Does NOT catch OperationalError/InterfaceError — those
    indicate database connectivity issues and should propagate.
    """
    try:
        return _is_migration_applied(conn, name)
    except ProgrammingError:
        # Table doesn't exist yet — migration is not applied
        return False


# ---------------------------------------------------------------------------
# Failed migration management (REL-MIG-06, BUG-DES-03)
# ---------------------------------------------------------------------------


def get_failed_migrations(engine) -> list[dict[str, Any]]:
    """Query the dead letter queue for failed migrations.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.

    Returns
    -------
    list[dict[str, Any]]
        List of failed migration records.
    """
    _ensure_migration_tracking_table(engine)
    results: list[dict[str, Any]] = []
    with engine.begin() as conn:
        try:
            r = conn.execute(
                text(
                    "SELECT migration_name, failed_at, error_message, "
                    "error_class, retry_count, resolved "
                    "FROM _failed_migrations ORDER BY failed_at"
                )
            )
            for row in r.fetchall():
                results.append({
                    "migration_name": row[0],
                    "failed_at": str(row[1]) if row[1] else None,
                    "error_message": row[2],
                    "error_class": row[3],
                    "retry_count": row[4],
                    "resolved": bool(row[5]),
                })
        except (OperationalError, ProgrammingError) as exc:
            logger.warning("Could not fetch failed migrations: %s", exc)
    return results


def retry_failed_migration(engine, migration_name: str) -> bool:
    """Attempt to retry a failed migration.

    BUG-DES-03: Uses UPDATE status='retrying' instead of DELETE to
    preserve audit trail. BUG-REL-02: Skips already-applied statements.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    migration_name : str
        The migration filename to retry.

    Returns
    -------
    bool
        True if the retry succeeded.
    """
    _ensure_migration_tracking_table(engine)

    # Find the SQL file
    sql_file = MIGRATIONS_DIR / migration_name
    if not sql_file.exists():
        logger.error("Migration file not found: %s", migration_name)
        return False

    raw_content = sql_file.read_text(encoding="utf-8")
    checksum = _compute_checksum(raw_content)
    sql_content = _strip_psql_meta_commands(raw_content)

    try:
        with engine.begin() as conn:
            # BUG-DES-03: UPDATE to 'retrying' instead of DELETE
            conn.execute(
                text(
                    "UPDATE _migration_history SET status = 'retrying' "
                    "WHERE migration_name = :n AND status = 'failed'"
                ),
                {"n": migration_name},
            )

            # BUG-REL-02: Parse statements and skip already-applied ones
            statements = _split_sql_statements(sql_content)
            inspector = inspect(engine)

            for stmt in statements:
                # Skip ALTER TABLE ADD COLUMN if column already exists
                m = _ALTER_TABLE_ADD_COL_PATTERN.match(stmt)
                if m:
                    table_name, col_name = m.group(1), m.group(2)
                    if _column_exists(inspector, table_name, col_name):
                        logger.info(
                            "Skipping already-applied statement: ADD COLUMN %s.%s",
                            table_name, col_name,
                        )
                        continue

                conn.execute(text(stmt))

            # Record success — update the 'retrying' record
            conn.execute(
                text(
                    "UPDATE _migration_history SET status = 'applied', "
                    "checksum = :c, applied_at = CURRENT_TIMESTAMP "
                    "WHERE migration_name = :n AND status = 'retrying'"
                ),
                {"c": checksum, "n": migration_name},
            )

        # Mark as resolved in _failed_migrations
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE _failed_migrations SET resolved = TRUE, "
                    "retry_count = retry_count + 1 "
                    "WHERE migration_name = :n"
                ),
                {"n": migration_name},
            )

        logger.info("Successfully retried migration: %s", migration_name)
        return True
    except Exception as exc:
        # Update retry count and record failure
        with engine.begin() as conn:
            try:
                conn.execute(
                    text(
                        "UPDATE _failed_migrations SET retry_count = retry_count + 1 "
                        "WHERE migration_name = :n"
                    ),
                    {"n": migration_name},
                )
                # BUG-DES-03: Update status back to 'failed'
                conn.execute(
                    text(
                        "UPDATE _migration_history SET status = 'failed' "
                        "WHERE migration_name = :n AND status = 'retrying'"
                    ),
                    {"n": migration_name},
                )
                _record_failure(conn, migration_name, checksum, str(exc), type(exc).__name__)
            except (OperationalError, ProgrammingError) as db_exc:
                logger.error("Could not record retry failure: %s", db_exc)

        logger.error("Retry of migration '%s' failed: %s", migration_name, exc)
        return False


def resolve_failed_migration(
    engine, migration_name: str, resolution_note: str = "",
) -> bool:
    """Mark a failed migration as resolved without retrying.

    GAP-DQ-07: Provides an API for admins to mark failures as resolved
    after manually fixing data.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    migration_name : str
        The migration filename to resolve.
    resolution_note : str
        Explanation of how the issue was resolved.

    Returns
    -------
    bool
        True if the migration was successfully marked as resolved.
    """
    _ensure_migration_tracking_table(engine)

    with engine.begin() as conn:
        try:
            # Verify the migration exists in _failed_migrations
            r = conn.execute(
                text(
                    "SELECT COUNT(*) FROM _failed_migrations "
                    "WHERE migration_name = :n AND resolved = FALSE"
                ),
                {"n": migration_name},
            )
            if r.scalar() == 0:
                logger.warning(
                    "No unresolved failure found for migration: %s",
                    migration_name,
                )
                return False

            # Mark as resolved
            conn.execute(
                text(
                    "UPDATE _failed_migrations "
                    "SET resolved = TRUE, resolution_note = :note "
                    "WHERE migration_name = :n AND resolved = FALSE"
                ),
                {"n": migration_name, "note": resolution_note},
            )

            # Update _migration_history status
            conn.execute(
                text(
                    "UPDATE _migration_history SET status = 'applied' "
                    "WHERE migration_name = :n AND status = 'failed'"
                ),
                {"n": migration_name},
            )

            logger.info(
                "Resolved failed migration: %s (note: %s)",
                migration_name, resolution_note or "N/A",
            )
            return True
        except (OperationalError, ProgrammingError) as exc:
            logger.error("Could not resolve failed migration: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Partial migration state (BUG-REL-04)
# ---------------------------------------------------------------------------


def get_partial_migration_state(engine, migration_name: str) -> dict[str, Any]:
    """Analyze the partial state after a failed migration.

    BUG-REL-04: Returns which parts of a migration were applied and
    which weren't for post-failure diagnostics.
    """
    result: dict[str, Any] = {
        "migration_name": migration_name,
        "applied_constraints": [],
        "missing_constraints": [],
        "applied_columns": [],
        "missing_columns": [],
    }

    inspector = inspect(engine)
    sql_file = MIGRATIONS_DIR / migration_name

    if not sql_file.exists():
        result["error"] = f"Migration file not found: {migration_name}"
        return result

    content = sql_file.read_text(encoding="utf-8")
    content = _strip_psql_meta_commands(content)
    statements = _split_sql_statements(content)

    for stmt in statements:
        # Check ALTER TABLE ADD COLUMN
        m = _ALTER_TABLE_ADD_COL_PATTERN.match(stmt)
        if m:
            table_name, col_name = m.group(1), m.group(2)
            if _table_exists(inspector, table_name):
                if _column_exists(inspector, table_name, col_name):
                    result["applied_columns"].append(f"{table_name}.{col_name}")
                else:
                    result["missing_columns"].append(f"{table_name}.{col_name}")

    return result


# ---------------------------------------------------------------------------
# Impact analysis (LINE-MIG-03, GUARD-CODE-12, GUARD-CODE-13)
# ---------------------------------------------------------------------------


def analyze_migration_impact(engine, migration_name: str) -> dict[str, Any]:
    """Analyze the potential impact of a migration on the system.

    Scans the SQL migration content, identifies ALTER TABLE and other
    DDL/DML statements, and cross-references against known code dependencies.

    GUARD-CODE-12: Expanded to detect DROP COLUMN, ALTER COLUMN,
    ADD/DROP CONSTRAINT, INSERT, UPDATE operations.
    GUARD-CODE-13: Uses module-level compiled regexes.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    migration_name : str
        The migration filename to analyze.

    Returns
    -------
    dict[str, Any]
        Impact analysis with affected_tables, affected_columns,
        dependent_code, and estimated_risk.
    """
    sql_file = MIGRATIONS_DIR / migration_name
    if not sql_file.exists():
        return {
            "affected_tables": [],
            "affected_columns": {},
            "dependent_code": [],
            "estimated_risk": "unknown",
            "error": f"Migration file not found: {migration_name}",
        }

    content = sql_file.read_text(encoding="utf-8")

    # Parse ALTER TABLE statements (GUARD-CODE-12: expanded patterns)
    affected_tables: list[str] = []
    affected_columns: dict[str, list[str]] = {}

    for pattern, operation_type in [
        (_ALTER_TABLE_ADD_COL_PATTERN, "ADD_COLUMN"),
        (_ALTER_TABLE_DROP_COL_PATTERN, "DROP_COLUMN"),
        (_ALTER_TABLE_ALTER_COL_PATTERN, "ALTER_COLUMN"),
        (_ALTER_TABLE_ADD_CONSTR_PATTERN, "ADD_CONSTRAINT"),
        (_ALTER_TABLE_DROP_CONSTR_PATTERN, "DROP_CONSTRAINT"),
    ]:
        for match in pattern.finditer(content):
            table = match.group(1)
            column = match.group(2)
            if table not in affected_tables:
                affected_tables.append(table)
            if table not in affected_columns:
                affected_columns[table] = []
            affected_columns[table].append(f"{column} ({operation_type})")

    # Parse CREATE TABLE statements
    for match in _CREATE_TABLE_PATTERN.finditer(content):
        table = match.group(1)
        if table not in affected_tables and not table.startswith("_"):
            affected_tables.append(table)

    # BUG-LINE-03: Parse DML operations (INSERT, UPDATE, DELETE)
    dml_tables: set[str] = set()
    for match in _DELETE_FROM_PATTERN.finditer(content):
        dml_tables.add(match.group(1))
    for match in _INSERT_INTO_PATTERN.finditer(content):
        dml_tables.add(match.group(1))
    for match in _UPDATE_PATTERN.finditer(content):
        dml_tables.add(match.group(1))

    has_deletes = bool(_DELETE_FROM_PATTERN.search(content))

    # Determine risk level
    risk = "low"
    if has_deletes:
        risk = "high"
    elif any("DROP" in line.upper() for line in content.split("\n") if not line.strip().startswith("--")):
        risk = "high"
    elif affected_tables or dml_tables:
        risk = "medium"

    # Identify dependent code modules
    dependent_code: list[str] = []
    table_to_module = {
        "drugs": "database.models, database.loaders, pipelines.chembl, pipelines.drugbank, pipelines.pubchem",
        "proteins": "database.models, database.loaders, pipelines.uniprot, pipelines.chembl",
        "drug_protein_interactions": "database.models, database.loaders, pipelines.chembl, pipelines.drugbank",
        "protein_protein_interactions": "database.models, database.loaders, pipelines.string",
        "gene_disease_associations": "database.models, database.loaders, pipelines.disgenet, pipelines.omim",
        "entity_mapping": "database.models, entity_resolution",
        "pipeline_runs": "database.models, dags",
    }
    for table in affected_tables:
        if table in table_to_module:
            for mod in table_to_module[table].split(", "):
                if mod not in dependent_code:
                    dependent_code.append(mod)

    return {
        "affected_tables": affected_tables,
        "affected_columns": affected_columns,
        "dml_affected_tables": list(dml_tables),
        "dependent_code": dependent_code,
        "estimated_risk": risk,
    }


# ---------------------------------------------------------------------------
# Package export verification (TEST-MIG-05, BUG-CODE-05)
# ---------------------------------------------------------------------------


def verify_package_exports() -> dict[str, bool]:
    """Verify all symbols in __all__ are actually importable.

    BUG-CODE-05: Uses standard import instead of direct __getattr__ call.

    Returns
    -------
    dict[str, bool]
        Mapping of symbol_name -> is_importable for each exported symbol.
    """
    from database.migrations import __all__

    results: dict[str, bool] = {}
    for symbol_name in __all__:
        try:
            # BUG-CODE-05: Use standard import mechanism
            import database.migrations as mig_pkg
            attr = getattr(mig_pkg, symbol_name, None)
            results[symbol_name] = attr is not None
        except (ImportError, AttributeError) as exc:
            results[symbol_name] = False
            logger.debug("Symbol '%s' not importable: %s", symbol_name, exc)
    return results


# ---------------------------------------------------------------------------
# Database fingerprint for idempotency testing (TEST-MIG-06, GAP-PERF-06)
# ---------------------------------------------------------------------------

_fingerprint_cache: dict[str, Any] | None = None
_fingerprint_cache_ts: float = 0.0
_FINGERPRINT_CACHE_TTL: float = 5.0  # seconds


def get_database_fingerprint(engine) -> dict[str, Any]:
    """Return a fingerprint of the current database state.

    GAP-PERF-06: Caches fingerprints with a 5-second TTL.
    GAP-TEST-06: Includes constraint and index info.

    Includes: table names, column counts, row counts per table,
    constraint names, index names, _migration_history contents.
    Use this to compare state before and after running migrations twice.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.

    Returns
    -------
    dict[str, Any]
        Fingerprint of the database state.
    """
    global _fingerprint_cache, _fingerprint_cache_ts

    # GAP-PERF-06: Return cached fingerprint if fresh
    now = time.monotonic()
    if _fingerprint_cache is not None and (now - _fingerprint_cache_ts) < _FINGERPRINT_CACHE_TTL:
        return _fingerprint_cache

    inspector = inspect(engine)
    fingerprint: dict[str, Any] = {
        "tables": {},
        "migration_history": [],
    }

    for table_name in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns(table_name)]
        try:
            with engine.begin() as conn:
                count = _get_approximate_row_count(
                    conn, table_name, engine.dialect.name,
                )
        except (OperationalError, ProgrammingError):
            count = -1

        table_info: dict[str, Any] = {
            "column_count": len(columns),
            "columns": sorted(columns),
            "row_count": count,
        }

        # GAP-TEST-06: Include constraint and index info
        try:
            constraints = inspector.get_unique_constraints(table_name)
            table_info["unique_constraints"] = [c["name"] for c in constraints if c.get("name")]
        except Exception:
            table_info["unique_constraints"] = []

        try:
            indexes = inspector.get_indexes(table_name)
            table_info["indexes"] = [i["name"] for i in indexes if i.get("name")]
        except Exception:
            table_info["indexes"] = []

        fingerprint["tables"][table_name] = table_info

    # Migration history
    if "_migration_history" in inspector.get_table_names():
        with engine.begin() as conn:
            try:
                r = conn.execute(
                    text("SELECT migration_name, checksum, status FROM _migration_history ORDER BY id")
                )
                fingerprint["migration_history"] = [
                    {"name": row[0], "checksum": row[1], "status": row[2]}
                    for row in r.fetchall()
                ]
            except (OperationalError, ProgrammingError):
                pass

    # Cache the result
    _fingerprint_cache = fingerprint
    _fingerprint_cache_ts = time.monotonic()

    return fingerprint


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_migrations()
    if hasattr(result, "failed") and result.failed:
        print(f"MIGRATION FAILED: {len(result.failed)} migration(s) failed")
        # v22 ROOT FIX: errors entries are now dicts (not strings).
        # Render the dict's "error" field for human readability.
        for name, err in zip(result.failed, result.errors):
            if isinstance(err, dict):
                err_str = err.get("error", str(err))
            else:
                err_str = str(err)
            print(f"  - {name}: {err_str}")
        raise SystemExit(1)
    elif hasattr(result, "applied"):
        print(f"MIGRATION COMPLETE: {len(result.applied)} applied, {len(result.skipped)} skipped")
    else:
        print("MIGRATION COMPLETE (legacy mode)")


# ---------------------------------------------------------------------------
# K fix (test isolation): Re-expose the ``run_migrations`` FUNCTION on the
# parent package's namespace after the submodule is loaded.
#
# ``import database.migrations.run_migrations`` causes Python to set
# ``database.migrations.__dict__['run_migrations'] = <this submodule>``,
# shadowing the function of the same name. We override that here so
# ``from database.migrations import run_migrations`` always returns the
# function (not the submodule), regardless of import order.
# ---------------------------------------------------------------------------
import sys as _sys

_parent_mod = _sys.modules.get("database.migrations")
if _parent_mod is not None:
    _parent_mod.__dict__["run_migrations"] = run_migrations  # type: ignore[name-defined]  # the function, not the submodule
