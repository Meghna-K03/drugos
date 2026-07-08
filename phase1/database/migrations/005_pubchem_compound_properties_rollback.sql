-- ============================================================================
-- Drug Repurposing ETL Platform — Rollback 005 PubChem Compound Properties
-- Migration: 005_pubchem_compound_properties_rollback.sql
-- Description: Drops the pubchem_compound_properties table and its indexes.
--
-- v22 ROOT FIX (audit P2-10 / Section 9): rollback sidecar for 005.
-- ============================================================================

BEGIN;

-- Drop indexes first (must precede table drop in some dialects).
DROP INDEX IF EXISTS idx_pubchem_props_cid;
DROP INDEX IF EXISTS idx_pubchem_props_inchikey;
DROP INDEX IF EXISTS idx_pubchem_props_is_deleted;
DROP INDEX IF EXISTS idx_pubchem_props_run_id;

-- Drop the table.
DROP TABLE IF EXISTS pubchem_compound_properties CASCADE;

COMMIT;
