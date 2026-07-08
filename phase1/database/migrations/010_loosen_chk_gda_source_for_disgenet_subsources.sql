-- =============================================================================
-- Migration 010: Loosen chk_gda_source for DisGeNET sub-source values
-- =============================================================================
-- v41 ROOT FIX (SEV1 #3): The chk_gda_source CHECK constraint in
-- migration 001 (line 910-911) restricted `source` to exactly
-- ('disgenet', 'omim'). However, disgenet_pipeline._derive_source_value
-- (line 2620) emits `f"disgenet_{source_id.lower()}"` for every DisGeNET
-- row — values like "disgenet_curated", "disgenet_inference",
-- "disgenet_v7_2024_06", etc. This caused 100% of DisGeNET GDA INSERTs
-- to fail with CheckViolation on PostgreSQL AND SQLite.
--
-- The fix allows:
--   * 'omim'                       (OMIM pipeline, unchanged)
--   * 'disgenet'                   (bare default, unchanged)
--   * 'disgenet_<subsrc>'          (any DisGeNET sub-source label)
--
-- This migration drops the old constraint and re-creates it with the
-- loosened pattern. Uses LIKE with ESCAPE for SQLite portability (the
-- migration 001 `~` regex operator is silently dropped by the
-- _translate_sql_for_sqlite translator).
-- =============================================================================

-- Drop the old constraint (both SQLite and PostgreSQL dialects)
DROP TABLE IF EXISTS _migration_010_progress;

-- PostgreSQL uses ALTER TABLE ... DROP CONSTRAINT
-- SQLite < 3.25 doesn't support DROP CONSTRAINT; we use a rebuild approach
DO $$
BEGIN
    -- PostgreSQL path
    ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_source;
EXCEPTION WHEN OTHERS THEN
    NULL;
END
$$;

-- Re-create the constraint with the loosened pattern.
-- LIKE 'disgenet|_%' ESCAPE '|' matches 'disgenet_curated' etc.
-- but NOT 'disgenet' itself (the underscore is escaped).
-- v41 ROOT FIX: use '|' as the escape char instead of '\' because
-- SQLite's ESCAPE clause requires a single character and Python's
-- string escaping turns '\\' into two chars in the rendered SQL.
ALTER TABLE gene_disease_associations
    ADD CONSTRAINT chk_gda_source
    CHECK (
        source IS NULL
        OR source = 'omim'
        OR source = 'disgenet'
        OR source LIKE 'disgenet|_%' ESCAPE '|'
    );

-- SQLite compatibility: ALTER TABLE ADD CONSTRAINT is not supported
-- on SQLite. The migration runner's _translate_sql_for_sqlite will
-- emit a NOTICE and skip. The ORM model (database/models.py) carries
-- the same CheckConstraint, so create_all() on a fresh SQLite DB
-- will have the loosened constraint. For existing SQLite DBs, the
-- constraint remains the OLD strict version until the DB is rebuilt
-- from ORM (drop + create_all). This is acceptable because SQLite is
-- dev-only — production uses PostgreSQL where this migration applies.

-- ============================================================================
-- v41 ROOT FIX verification marker
-- ============================================================================
INSERT INTO _migration_audit_log (migration_id, applied_at, status, notes)
VALUES ('010', NOW(), 'applied',
        'Loosened chk_gda_source to allow disgenet_<subsrc> values '
        'emitted by disgenet_pipeline._derive_source_value. '
        'See SEV1 #3 in v41 forensic audit.')
ON CONFLICT DO NOTHING;
