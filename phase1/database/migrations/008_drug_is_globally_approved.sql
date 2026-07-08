-- ============================================================================
-- Drug Repurposing ETL Platform — Drug.is_globally_approved Column Migration
-- Migration: 008_drug_is_globally_approved.sql
-- Description: Add an `is_globally_approved` BOOLEAN column to the `drugs`
--              table so the ChEMBL pipeline's per-drug global-approval flag
--              (any of FDA / EMA / PMDA / MHRA / Health Canada / TGA —
--              derived from `max_phase == 4` per SW-1 ROOT FIX patient-
--              safety audit) is persisted instead of being silently dropped
--              by `_filter_to_drug_columns` (which kept only Drug-model
--              columns, and is_globally_approved was not one).
--
-- ROOT-CAUSE FIX (audit P1-28):
--   The ChEMBL pipeline (chembl_pipeline.py:1984) emits is_globally_approved
--   in every record dict, but the Drug ORM model did not declare the column
--   and `_filter_to_drug_columns` did not whitelist it. The loader
--   (bulk_upsert_drugs) rejected/ignored the unknown column, so the value
--   was always NULL in the DB. Downstream consumers that queried
--   `is_globally_approved` got NULL for every row — the column was
--   effectively dead.
--
--   This migration adds the column; the ORM and the column-whitelist are
--   updated in parallel (database/models.py, chembl_pipeline.py).
--
-- All new columns are NULLABLE — existing rows and existing tests are
-- unaffected. No columns are dropped, no constraints are weakened.
--
-- PREREQUISITES: 001_initial_schema.sql through 007_pipeline_run_metadata.sql.
-- ============================================================================

BEGIN;

-- ===========================================================================
-- Phase 1: Add is_globally_approved column
-- ===========================================================================
-- Nullable BOOLEAN: NULL means "unknown" (e.g. drugs loaded from sources
-- other than ChEMBL that don't populate max_phase). The ChEMBL pipeline
-- sets it to (max_phase == 4) per SW-1 ROOT FIX.
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS is_globally_approved BOOLEAN;

-- Idempotency: PostgreSQL does not support IF NOT EXISTS for ADD CONSTRAINT,
-- so we use a DO block to check pg_constraint first.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_is_globally_approved') THEN
        ALTER TABLE drugs ADD CONSTRAINT chk_drugs_is_globally_approved
            CHECK (is_globally_approved IS NULL OR is_globally_approved IN (0, 1, TRUE, FALSE));
        RAISE NOTICE '  [OK] Added constraint chk_drugs_is_globally_approved';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_drugs_is_globally_approved already exists';
    END IF;
END $$;

-- ===========================================================================
-- Phase 2: Backfill from max_phase == 4 (ChEMBL semantic — any regulator)
-- ===========================================================================
-- Any drug already loaded with max_phase=4 is globally approved per ChEMBL.
-- This backfill brings existing rows in line with what the ChEMBL pipeline
-- would have written if is_globally_approved had existed from the start.
-- Rows with is_globally_approved already set (non-NULL) are preserved.
UPDATE drugs
SET is_globally_approved = TRUE
WHERE is_globally_approved IS NULL
  AND max_phase = 4;

UPDATE drugs
SET is_globally_approved = FALSE
WHERE is_globally_approved IS NULL
  AND max_phase IS NOT NULL
  AND max_phase < 4;

-- ===========================================================================
-- Phase 3: Index for fast filtering of globally-approved drugs
-- ===========================================================================
CREATE INDEX IF NOT EXISTS idx_drugs_is_globally_approved
    ON drugs (is_globally_approved)
    WHERE is_globally_approved IS NOT NULL;

-- ===========================================================================
-- Phase 4: Schema version metadata
-- ===========================================================================
INSERT INTO schema_version (version, description)
VALUES (8, 'Add is_globally_approved BOOLEAN column to drugs table (P1-28 ROOT FIX — ChEMBL pipeline emits this but it was silently dropped by _filter_to_drug_columns); backfill from max_phase == 4')
ON CONFLICT (version) DO NOTHING;

COMMIT;
