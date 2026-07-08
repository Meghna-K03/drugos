-- ============================================================================
-- Drug Repurposing ETL Platform — Rollback 009 Tighten InChIKey CHECK
-- Migration: 009_tighten_inchikey_check_constraint_rollback.sql
-- Description: Reverses the constraint swap from
--              009_tighten_inchikey_check_constraint.sql by restoring
--              the original over-permissive CHECK from
--              001_initial_schema.sql (lines 225-236).
--
-- v28 ROOT FIX (audit TOP-17): rollback sidecar for 009.
--
-- WARNING: restoring the old constraint RE-INTRODUCES the silent
-- Python-vs-SQL divergence that 009 fixed. Operators should only run
-- this rollback if they have an explicit reason (e.g. a downstream
-- consumer that depends on TEST/OUTER/INNER/IK% identifiers reaching
-- the database). Rolling back WITHOUT fixing the Python side will
-- re-open audit finding P1-ER-3.
--
-- NOTES:
--   - 009 did NOT modify any data rows; only the constraint. This
--     rollback therefore also does not touch data.
--   - The schema_version row inserted by 009 is NOT removed (downgrade
--     of version metadata is reserved for a full 009_rollback if ever
--     needed; this sidecar focuses on the constraint only, mirroring
--     the 002_rollback pattern).
-- ============================================================================

BEGIN;

-- Reverse the constraint swap (009 dropped the old, added the new).
-- Drop the tightened constraint and restore the original verbatim from
-- 001_initial_schema.sql so a rollback returns the schema to byte-exact
-- parity with the pre-009 state.
ALTER TABLE drugs DROP CONSTRAINT IF EXISTS chk_drugs_inchikey_format;

-- Restore the ORIGINAL constraint from 001_initial_schema.sql
-- (lines 225-236). This is the verbatim text from 001 — including the
-- TEST/OUTER/INNER/IK% clauses that 009 removed. Restoring them is the
-- correct rollback semantics: undo the change, do not partially revert.
ALTER TABLE drugs
    ADD CONSTRAINT chk_drugs_inchikey_format
    CHECK (
        LENGTH(inchikey) = 27
        OR inchikey LIKE 'SYNTH%'
        OR inchikey LIKE 'TEST%'
        OR inchikey LIKE 'OUTER%'
        OR inchikey LIKE 'INNER%'
        OR (LENGTH(inchikey) <= 30 AND inchikey LIKE 'IK%')
    );

COMMENT ON CONSTRAINT chk_drugs_inchikey_format ON drugs IS
    'InChIKey format: original over-permissive CHECK from 001_initial_schema.sql '
    '(27-char canonical OR SYNTH/TEST/OUTER/INNER/IK% prefixes). '
    'v28 audit TOP-17 rollback: re-introduces the Python-vs-SQL divergence '
    'that 009 fixed — only run if a downstream consumer depends on the '
    'extra prefix clauses.';

RAISE NOTICE '  [OK] Rolled back chk_drugs_inchikey_format to 001 original (over-permissive)';

COMMIT;
