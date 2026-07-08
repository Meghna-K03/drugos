"""
Declarative base class and reusable mixins for the Drug Repurposing ETL platform.

This module is the **single canonical definition** of ``Base``, shared by both
``database.connection`` and ``database.models``.  Extracting Base from
connection.py eliminates the circular-import risk identified in ARCH-02.

Architecture
------------
``database.base``  →  ``database.connection`` (imports Base)
                  →  ``database.models``    (imports Base, mixins)

No module imports from a downstream consumer, so the dependency graph is
strictly acyclic.

Mixins Provided
---------------
- **IDMixin**         — Auto-incrementing integer primary key.
- **TimestampMixin**  — ``created_at`` and ``updated_at`` with server-side
  defaults and a PostgreSQL trigger for ``updated_at`` (onupdate does NOT
  fire for bulk operations — IDEM-02).
- **SoftDeleteMixin** — ``is_deleted`` and ``deleted_at`` for reversible
  deletes without cascade destruction (DES-08, REL-01).

Naming Convention (CMP-04)
--------------------------
A ``naming_convention`` dictionary is attached to ``Base.metadata`` so that
all constraints (CHECK, UNIQUE, FK, PK) receive deterministic, predictable
names.  This is required for:
  * Idempotent ``ALTER TABLE ... ADD CONSTRAINT`` in migrations.
  * Cross-dialect consistency between PostgreSQL and SQLite.
  * Automated schema-diff tooling.

Schema Version (ARCH-07)
------------------------
``SCHEMA_VERSION`` is the single source of truth for the current ORM schema
revision.  It must be incremented whenever a migration file is added.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# Schema version — auto-derived from migration file names at import time.
# v35 ROOT FIX (issue 32): previously this was a hardcoded constant that
# had to be bumped manually whenever a new migration file was added. The
# v29 ROOT FIX bumped it from 6 to 9, but if a new migration ``010_*.sql``
# is added, the constant would silently fall behind and
# ``check_migrations()`` would report ``schema_version_matches=False``
# forever (the exact bug the v29 fix was supposed to prevent).
#
# The fix: scan the migrations directory for files matching ``NNN_*.sql``
# (excluding rollback files ``*_rollback.sql``) and take the max NNN.
# This is O(N) at import time where N = number of migrations (~9), so the
# cost is negligible. The migrations directory is resolved relative to
# this file (``database/base.py`` → ``database/migrations/``) so the
# derivation works regardless of the current working directory.
# ---------------------------------------------------------------------------
def _derive_schema_version() -> int:
    """Return the highest migration version found in the migrations dir.

    Looks for files named ``NNN_*.sql`` (where NNN is 1-3 digits) in
    ``database/migrations/``, EXCLUDING rollback files
    (``*_rollback.sql``). Returns the maximum NNN found, or ``0`` if the
    directory is missing or empty (e.g., test isolation).
    """
    migrations_dir = Path(__file__).resolve().parent / "migrations"
    if not migrations_dir.is_dir():
        return 0
    pattern = re.compile(r"^(\d{1,3})_[^_].*\.sql$")
    versions: list[int] = []
    for path in migrations_dir.iterdir():
        if not path.is_file():
            continue
        if path.name.endswith("_rollback.sql"):
            continue
        m = pattern.match(path.name)
        if m:
            versions.append(int(m.group(1)))
    return max(versions) if versions else 0


SCHEMA_VERSION: int = _derive_schema_version()
# Safety floor: if the migrations dir was missing or empty (e.g., test
# isolation with a stripped-down install), fall back to 10 (the highest
# known migration version as of v43 — migration 010 loosened
# chk_gda_source for DisGeNET sub-source values). This prevents the
# schema-version constant from being 0 — which would break
# ``check_migrations()``.
# v43 ROOT FIX (P1-032): bumped from 9 to 10 to reflect migration 010.
if SCHEMA_VERSION == 0:
    SCHEMA_VERSION = 10

# ---------------------------------------------------------------------------
# Naming convention for all constraints (CMP-04)
# ---------------------------------------------------------------------------
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "chk_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# ===========================================================================
# BASE CLASS
# ===========================================================================


class Base(DeclarativeBase):
    """Declarative base class shared by all ORM models.

    Every model in ``database.models`` inherits from this class so that
    ``Base.metadata.create_all(engine)`` creates all tables at once.

    The ``metadata.naming_convention`` ensures deterministic constraint names
    across PostgreSQL and SQLite (CMP-04).
    """
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ===========================================================================
# MIXINS
# ===========================================================================


class IDMixin:
    """Auto-incrementing integer primary key.

    [ARCH-05] Centralises the ``id`` column so every model inherits
    consistently instead of re-declaring it.
    """

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )


class TimestampMixin:
    """``created_at`` and ``updated_at`` timestamps with server defaults.

    [ARCH-05] Centralises timestamp columns.
    [DESM-06] Adds ``updated_at`` to all models (Protein previously lacked it).
    [IDEM-02] ``onupdate`` is NOT set because it does not fire for bulk
    operations.  PostgreSQL uses a trigger (defined in migration SQL) and
    loaders must explicitly set ``updated_at`` in ``updatable_cols``.
    """

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Reversible soft-delete pattern (DES-08, REL-01).

    Instead of hard-deleting rows (which cascades destructively), set
    ``is_deleted = True`` and optionally record ``deleted_at``.  Downstream
    queries should filter ``WHERE is_deleted = FALSE``.

    Applied to ``Drug`` and ``Protein`` — the two primary entity tables
    where accidental data loss is most impactful.
    """

    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        server_default="0",
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def soft_delete(self) -> None:
        """Mark this record as soft-deleted with a timestamp."""
        self.is_deleted = True
        self.deleted_at = datetime.datetime.now(datetime.timezone.utc)

    def restore(self) -> None:
        """Undo a soft delete."""
        self.is_deleted = False
        self.deleted_at = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "Base",
    "IDMixin",
    "NAMING_CONVENTION",
    "SCHEMA_VERSION",
    "SoftDeleteMixin",
    "TimestampMixin",
]
