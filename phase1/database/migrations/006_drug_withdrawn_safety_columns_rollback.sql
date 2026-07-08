-- ============================================================================
-- Drug Repurposing ETL Platform — Rollback 006 Drug Withdrawn/Safety Columns
-- Migration: 006_drug_withdrawn_safety_columns_rollback.sql
-- Description: Reverses the ALTER TABLE changes from
--              006_drug_withdrawn_safety_columns.sql.
--
-- v22 ROOT FIX (audit P2-10 / Section 9): rollback sidecar for 006.
-- ============================================================================

BEGIN;

-- Drop indexes created by 006.
DROP INDEX IF EXISTS idx_drugs_is_withdrawn;
DROP INDEX IF EXISTS idx_drugs_clinical_status;
DROP INDEX IF EXISTS idx_drugs_cas_number;

-- Drop the trigger that 006 created (IF EXISTS guards make this safe).
DROP TRIGGER IF EXISTS trg_drugs_sync_withdrawn ON drugs;
DROP FUNCTION IF EXISTS fn_sync_withdrawn_status() CASCADE;

-- Drop columns added by 006.
ALTER TABLE drugs DROP COLUMN IF EXISTS is_withdrawn;
ALTER TABLE drugs DROP COLUMN IF EXISTS clinical_status;
ALTER TABLE drugs DROP COLUMN IF EXISTS cas_number;
ALTER TABLE drugs DROP COLUMN IF EXISTS logp;
ALTER TABLE drugs DROP COLUMN IF EXISTS tpsa;
ALTER TABLE drugs DROP COLUMN IF EXISTS h_bond_donor_count;
ALTER TABLE drugs DROP COLUMN IF EXISTS h_bond_acceptor_count;
ALTER TABLE drugs DROP COLUMN IF EXISTS rotatable_bond_count;
ALTER TABLE drugs DROP COLUMN IF EXISTS heavy_atom_count;
ALTER TABLE drugs DROP COLUMN IF EXISTS complexity;
ALTER TABLE drugs DROP COLUMN IF EXISTS completeness_score;
ALTER TABLE drugs DROP COLUMN IF EXISTS groups;

COMMIT;
