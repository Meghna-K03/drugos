-- ============================================================================
-- Drug Repurposing ETL Platform — Rollback 008 Drug.is_globally_approved
-- Migration: 008_drug_is_globally_approved_rollback.sql
-- Description: Reverses the ALTER TABLE changes from
--              008_drug_is_globally_approved.sql.
--
-- ROOT-CAUSE FIX (audit P1-28): rollback sidecar for 008.
-- ============================================================================

BEGIN;

-- Drop the partial index created by 008.
DROP INDEX IF EXISTS idx_drugs_is_globally_approved;

-- Drop the column added by 008.
ALTER TABLE drugs DROP COLUMN IF EXISTS is_globally_approved;

COMMIT;
