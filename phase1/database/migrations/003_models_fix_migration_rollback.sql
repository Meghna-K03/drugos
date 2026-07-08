-- ============================================================================
-- Drug Repurposing ETL Platform — Rollback 003 Models Fix
-- Migration: 003_models_fix_migration_rollback.sql
-- Description: Reverses the ALTER TABLE changes from
--              003_models_fix_migration.sql.
--
-- v22 ROOT FIX (audit P2-10 / Section 9): rollback sidecar for 003.
--
-- v29 ROOT FIX (audit D-13): rollback was a no-op. Now actually undoes
-- the forward migration. The previous rollback dropped ONE index
-- (idx_gda_uniprot_id — which is also created by 001, so dropping it
-- broke 001's contract!) and explicitly left every other schema change
-- from 003 in place with a comment that the columns are "depended on by
-- ORM models" so they should not be dropped. The net effect was that
-- running the rollback gave operators a false sense of safety — they
-- thought 003 had been undone, but only one index was dropped (and that
-- drop was itself incorrect because 001 owns the index).
--
-- This version is carefully scoped to undo ONLY the schema objects that
-- 003 actually adds or changes BEYOND what 001 already defines:
--
--   * 003 DROPs 001's narrow ``chk_drugs_inchikey_format`` (CHECK:
--     LENGTH=27 OR LIKE 'SYNTH%') and re-ADDs a wider version that also
--     accepts TEST%, OUTER%, INNER%, and IK% prefixes up to 30 chars.
--     This is a REAL schema change. The rollback DROPs 003's wider
--     version and re-ADDs 001's narrow version.
--
--   * 003 INSERTs a row into ``schema_version`` (version=3). The
--     ``schema_version`` table itself is OWNED by 001 (001 creates it
--     and inserts version=1), so the rollback does NOT drop the table —
--     it DELETEs 003's row.
--
--   * 003 makes DATA changes (deletes misordered PPI rows, swaps
--     misordered PPI pairs, deletes NULL PPI pairs) that CANNOT be
--     reversed without a backup. The rollback documents this and
--     leaves the data in its post-003 state. Operators who need the
--     pre-003 PPI data must restore from a backup before running this
--     rollback.
--
-- What the rollback DOES NOT drop (and why):
--
--   * The CHECK constraints 003 "adds" via ``IF NOT EXISTS`` (e.g.
--     chk_drugs_max_phase, chk_ppi_*, chk_drugs_is_fda_approved,
--     chk_proteins_uniprot_length, chk_gda_disease_id_type,
--     chk_pipeline_runs_*, chk_dpi_activity_value_positive, etc.) are
--     ALL also created by 001_initial_schema.sql. 001 owns them. 003
--     is a defensive re-assertion in case 001 wasn't applied. Dropping
--     them here would break 001's contract.
--
--   * The partial unique indexes 003 "creates" via ``IF NOT EXISTS``
--     (uq_drugs_chembl_id, uq_drugs_drugbank_id,
--     uq_entity_mapping_name_no_inchikey, uq_entity_mapping_chembl,
--     uq_entity_mapping_drugbank) are ALL also created by 001.
--
--   * The non-unique indexes 003 "creates" via ``IF NOT EXISTS``
--     (idx_gda_uniprot_id, idx_dpi_protein_interaction,
--     idx_dpi_drug_interaction) are ALL also created by 001.
--
--   * The columns 003 "adds" via ``ADD COLUMN IF NOT EXISTS``
--     (updated_at, is_deleted, deleted_at, source_version,
--     source_fetch_date, entity_resolved, pipeline_run_id, score_type,
--     score_method, score_json, match_history, disease_id_type) are
--     ALL also created by 001.
--
--   * The type changes 003 makes (inchikey to VARCHAR(50),
--     uniprot_id to VARCHAR(10), molecular_weight to NUMERIC(12,6),
--     pmid_list to VARCHAR(2000), error_message to VARCHAR(500),
--     canonical_inchikey to VARCHAR(50)) are ALL already in 001. 003's
--     ALTER COLUMN TYPE statements are idempotent no-ops.
--
--   * The ``source_id`` NOT NULL / DEFAULT change 003 makes on
--     drug_protein_interactions is a no-op because 001 already has
--     source_id as nullable (no NOT NULL constraint, no DEFAULT).
--
--   * The ``schema_version`` table is created by 001 (line 67). 003's
--     ``CREATE TABLE IF NOT EXISTS schema_version`` is a no-op.
--     Dropping the table here would break 001's contract.
--
-- Net effect: after running this rollback, the schema matches the
-- state immediately after 001 was applied (before 003 was applied),
-- EXCEPT for the irreversible PPI data changes which are documented
-- above. Operators who need a full data reset should restore from a
-- pre-003 backup.
-- ============================================================================

BEGIN;

-- v29 ROOT FIX (audit D-13): actually undo 003's REAL schema changes.

-- 1. Revert chk_drugs_inchikey_format to 001's narrow version.
--    003 DROPs 001's narrow version (CHECK: LENGTH=27 OR LIKE 'SYNTH%')
--    and re-ADDs a wider version that also accepts TEST%, OUTER%,
--    INNER%, and IK% prefixes up to 30 chars. The wider version allows
--    synthetic/test InChIKeys that 001's narrow version rejects. To
--    undo, DROP 003's wider version and re-ADD 001's narrow version.
--    Idempotent guard: only DROP if the constraint exists.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_inchikey_format') THEN
        ALTER TABLE drugs DROP CONSTRAINT chk_drugs_inchikey_format;
        RAISE NOTICE '  [OK] Dropped 003-wide chk_drugs_inchikey_format';
    END IF;
    -- Re-add 001's narrow version.
    ALTER TABLE drugs ADD CONSTRAINT chk_drugs_inchikey_format
        CHECK (LENGTH(inchikey) = 27 OR inchikey LIKE 'SYNTH%');
    RAISE NOTICE '  [OK] Re-added 001-narrow chk_drugs_inchikey_format (003 rollback)';
END $$;

-- 2. Delete 003's row from schema_version. 001 owns the schema_version
--    table (and inserts version=1). 003 inserts version=3. Removing
--    this row restores the schema_version table to its post-001 state.
--    Note: this does NOT drop the schema_version table — that is owned
--    by 001.
DELETE FROM schema_version WHERE version = 3;

-- 3. IRREVERSIBLE DATA CHANGES (documented, not undone):
--    003 performs the following DATA changes that cannot be reversed
--    without a backup:
--      a) DELETEs misordered PPI rows where protein_a_id > protein_b_id
--         AND a symmetric (protein_b_id, protein_a_id) row exists
--         (keeps the ordered copy).
--      b) UPDATEs misordered PPI rows to swap protein_a_id and
--         protein_b_id (after the symmetric-duplicate DELETE above).
--      c) DELETEs PPI rows where protein_a_id IS NULL or
--         protein_b_id IS NULL.
--    These data changes are not undoable because the original row data
--    is gone. Operators who need the pre-003 PPI data must restore
--    from a backup taken before 003 was applied.

COMMIT;
