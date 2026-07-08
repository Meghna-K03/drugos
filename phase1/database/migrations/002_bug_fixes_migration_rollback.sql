-- ============================================================================
-- Drug Repurposing ETL Platform — Rollback 002 Bug Fixes
-- Migration: 002_bug_fixes_migration_rollback.sql
-- Description: Reverses the column/constraint changes from
--              002_bug_fixes_migration.sql.
--
-- v22 ROOT FIX (audit P2-10 / Section 9): rollback sidecar for 002.
--
-- v29 ROOT FIX (audit D-13): rollback was a no-op. Now actually undoes
-- the forward migration. The previous rollback only dropped and re-added
-- the chk_audit_log_operation constraint — but 002's forward migration
-- drop+re-add of that constraint is itself a no-op (001 already has the
-- same whitelist), so the rollback was undoing a no-op with a no-op.
-- Meanwhile 002's REAL schema changes (the new COALESCE unique index on
-- GDA, the renamed GDA unique constraint, the dedup-archive table, the
-- dropped original uq_gda_gene_disease_source constraint) were left
-- untouched — the rollback was effectively a no-op. This version drops
-- every schema object 002 created and restores every object 002 dropped,
-- so the schema after rollback matches the schema before 002 was applied.
--
-- NOTES:
--   - 002 added `audit_log.row_count`, `audit_log.details`. These columns
--     are ALSO in 001 (migration 002 uses `ADD COLUMN IF NOT EXISTS`
--     defensively because 001 already added them). They are OWNED by 001,
--     NOT by 002 — dropping them here would break 001's contract. They
--     are intentionally left in place.
--   - 002 added `proteins.gene_symbol`, `proteins.protein_name`,
--     `proteins.function_desc` — but again these are ALSO in 001
--     (002 uses `IF NOT EXISTS`). They are OWNED by 001, not by 002.
--     Left in place.
--   - 002 created `ix_gda_dedup_temp` (transient) but DROPPED it within
--     the same migration. Nothing to undo.
--   - 002 DROPPED `uq_entity_mapping_inchikey` (the 001 version) and
--     RE-CREATED it with an identical definition (partial unique index
--     `WHERE canonical_inchikey IS NOT NULL`). The net change is zero,
--     so no rollback action is needed for this index.
--   - 002 drop+re-add of `chk_audit_log_operation` is a no-op (same
--     whitelist in 001 and 002). No rollback action needed.
--   - 002's data changes (NULL cleanup, deduplication of GDA rows,
--     archiving of duplicates into _migration_002_dedup_archive) CANNOT
--     be reversed without restoring from backup — the original rows
--     were DELETEd. The dedup archive table is preserved here so an
--     operator can manually restore archived rows if needed. Documented
--     as a known limitation.
-- ============================================================================

BEGIN;

-- v29 ROOT FIX (audit D-13): actually undo 002's schema changes.

-- 1. Drop the COALESCE-based unique index 002 created on
--    gene_disease_associations. This index did not exist before 002.
DROP INDEX IF EXISTS uq_gene_disease_associations_gda_coalesced;

-- 2. Drop the renamed unique constraint 002 added on
--    gene_disease_associations. 002 added
--    `uq_gene_disease_associations_gene_symbol_disease_id_source` after
--    dropping 001's `uq_gda_gene_disease_source`. To undo, we drop
--    002's constraint and re-add 001's.
ALTER TABLE gene_disease_associations
    DROP CONSTRAINT IF EXISTS uq_gene_disease_associations_gene_symbol_disease_id_source;

-- 3. Re-add the ORIGINAL constraint from 001_initial_schema.sql (line 914).
--    002 dropped this when it replaced it with the renamed version.
--    Idempotent guard: only re-add if it doesn't already exist (in case
--    the rollback is run on a DB where 001 was applied but 002 wasn't).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_gda_gene_disease_source'
    ) THEN
        ALTER TABLE gene_disease_associations
            ADD CONSTRAINT uq_gda_gene_disease_source
            UNIQUE (gene_symbol, disease_id, source);
        RAISE NOTICE '  [OK] Re-added constraint uq_gda_gene_disease_source (002 rollback)';
    ELSE
        RAISE NOTICE '  [SKIP] constraint uq_gda_gene_disease_source already exists';
    END IF;
END $$;

-- 4. Drop the _migration_002_dedup_archive table 002 created. This
--    table held archived duplicates from the GDA dedup operation. The
--    archived JSON data is preserved in the table until this rollback
--    runs — dropping it here is the schema-undo; if an operator needs
--    to recover the archived rows, they should dump the table BEFORE
--    running this rollback. (See the NOTES section above.)
DROP TABLE IF EXISTS _migration_002_dedup_archive;

-- 5. chk_audit_log_operation: 002's drop+re-add is a no-op (same
--    whitelist as 001). No action needed — leaving the constraint in
--    its 001/002 state. Previous versions of this rollback did a
--    drop+re-add that "restored" the original whitelist, but the
--    "original" whitelist and the "002" whitelist are identical, so
--    the drop+re-add was a no-op. Removed to avoid confusion.

COMMIT;
