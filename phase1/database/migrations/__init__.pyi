"""Type stubs for database.migrations package.

PEP 561 type checking support. These type declarations enable static
type checking (mypy, pyright) for code that imports from the
database.migrations package via the lazy-loading facade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from sqlalchemy import Engine

# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------
__version__: str
__all__: list[str]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MigrationConfig:
    migrations_dir: Path | None
    dry_run: bool
    batch_size: int
    timeout_seconds: int
    skip_migrations: set[str] | None
    require_checksum: bool
    concurrent_indexes: bool
    interactive: bool
    stop_on_failure: bool
    max_retries: int
    retry_backoff_base: float
    verify_data_checksums: bool
    allow_destructive_sql: bool
    on_migration_start: Callable | None
    on_migration_complete: Callable | None
    on_migration_fail: Callable | None
    correlation_id: str | None
    pipeline_name: str | None
    run_id: str | None
    pipeline_run_id: int | None

    @classmethod
    def from_env(cls) -> MigrationConfig: ...

@dataclass(frozen=True)
class MigrationResult:
    applied: list[str]
    skipped: list[str]
    failed: list[str]
    total_duration_seconds: float
    dialect: str
    schema_version_before: int | None
    schema_version_after: int | None
    row_count_changes: dict[str, tuple[int, int]]
    data_checksums: dict[str, str]
    errors: list[str]

@dataclass(frozen=True)
class MigrationHealthResult:
    all_applied: bool
    applied_count: int
    pending_count: int
    applied_migrations: list[str]
    pending_migrations: list[str]
    schema_version_matches: bool
    dialect: str

@dataclass(frozen=True)
class MigrationStatus:
    applied_migrations: list[dict[str, Any]]
    pending_migrations: list[str]
    total_migrations: int
    schema_version_code: int
    schema_version_db: int | None

@dataclass(frozen=True)
class MigrationMetrics:
    total_migrations: int
    applied_count: int
    skipped_count: int
    failed_count: int
    total_duration_seconds: float
    per_migration_timing: dict[str, float]
    dialect: str

class MigrationError(Exception):
    failed: list[str]
    errors: list[Exception]
    def __init__(self, failed: list[str], errors: list[Exception]) -> None: ...

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MIGRATIONS_DIR: Path
REQUIRED_COLUMNS: dict[str, list[tuple[str, str]]]
SCHEMA_VERSION: int
INCHIKEY_MAX_LENGTH: int
STANDARD_INCHIKEY_LENGTH: int
SYNTHETIC_INCHIKEY_PREFIX: str
STRING_SCORE_MIN: int
STRING_SCORE_MAX: int
MOLECULAR_WEIGHT_PRECISION: int
DIALECT_POSTGRESQL: str
DIALECT_SQLITE: str
SUPPORTED_DIALECTS: frozenset[str]
MIGRATION_NAME_MAX_LENGTH: int
MIGRATION_FILENAME_PATTERN: str
MIGRATION_BATCH_SIZE: int
PLANNED_MIGRATION_FRAMEWORK: str
SQL_IDENTIFIER_RE: re.Pattern[str]

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

def run_migrations(
    engine: Engine | None = ...,
    config: MigrationConfig | None = ...,
) -> MigrationResult: ...

def check_migrations(engine: Engine | None = ...) -> MigrationHealthResult: ...

def get_migration_status(engine: Engine | None = ...) -> MigrationStatus: ...

def get_sql_migration_files() -> list[Path]: ...

def get_migration_runner() -> Callable: ...

def rollback_migration(migration_name: str, engine: Engine | None = ...) -> None: ...

def validate_scientific_constraints(engine: Engine) -> list[str]: ...

def validate_migration_config(config: MigrationConfig | None = ...) -> list[str]: ...

def verify_schema_matches_orm(engine: Engine) -> dict[str, Any]: ...

def verify_package_exports() -> dict[str, bool]: ...

def get_database_fingerprint(engine: Engine) -> dict[str, Any]: ...

def plan_migrations(engine: Engine | None = ..., config: MigrationConfig | None = ...) -> list[dict[str, Any]]: ...

# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

def create_test_migrations_dir(tmp_path: Path) -> Path: ...

def reset_migration_state(engine: Engine) -> None: ...

def count_applied_migrations(engine: Engine) -> int: ...

def get_migration_checksum(engine: Engine, name: str) -> str | None: ...

def verify_table_schema(engine: Engine, table_name: str, expected_columns: list[str]) -> bool: ...

def get_failed_migrations(engine: Engine) -> list[dict[str, Any]]: ...

def retry_failed_migration(engine: Engine, migration_name: str) -> bool: ...

def analyze_migration_impact(engine: Engine, migration_name: str) -> dict[str, Any]: ...

# ---------------------------------------------------------------------------
# Deprecated
# ---------------------------------------------------------------------------

def run_migration_002(
    engine: Engine | None = ...,
    config: MigrationConfig | None = ...,
) -> MigrationResult: ...
