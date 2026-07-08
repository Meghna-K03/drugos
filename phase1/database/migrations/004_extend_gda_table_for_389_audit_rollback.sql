-- ============================================================================
-- Drug Repurposing ETL Platform — Rollback 004 GDA Extension
-- Migration: 004_extend_gda_table_for_389_audit_rollback.sql
-- Description: Reverses the ALTER TABLE changes from
--              004_extend_gda_table_for_389_audit.sql.
--
-- v22 ROOT FIX (audit P2-10 / Section 9): rollback sidecar for 004.
-- FIX TOP-4 (FIX-CFG-ML audit): the previous rollback dropped
-- ``audit_389_*`` columns that the forward migration NEVER ADDS —
-- the rollback therefore dropped zero columns, leaving the full 004
-- schema in place even after a "rollback" was invoked. Operators could
-- not trust the rollback framework. This rewritten rollback drops
-- EXACTLY the columns that 004_extend_gda_table_for_389_audit.sql adds,
-- in reverse declaration order, using ``DROP COLUMN IF EXISTS`` for
-- idempotency. It also drops the indexes (which depend on those
-- columns) and the ``dead_letter_gda`` table that 004 creates.
-- ============================================================================

BEGIN;

-- ===========================================================================
-- Phase 1: drop the dead_letter_gda table created by 004 (DQ-18 / REL-3).
-- ===========================================================================
DROP TABLE IF EXISTS dead_letter_gda CASCADE;

-- ===========================================================================
-- Phase 2: drop the indexes created by 004 (these reference the new
-- columns and would otherwise block the column drop on some PG versions).
-- ===========================================================================
DROP INDEX IF EXISTS idx_gda_gene_id;
DROP INDEX IF EXISTS idx_gda_source_id;
DROP INDEX IF EXISTS idx_gda_snapshot_tag;

-- ===========================================================================
-- Phase 3: drop the CHECK constraints added by 004 (these reference the
-- new columns).
-- ===========================================================================
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_disease_id_type;
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_disease_type;
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_confidence_tier;
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_evidence_strength;
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_year_range;
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_normalized_score_range;

-- ===========================================================================
-- Phase 4: drop the columns added by 004 (reverse declaration order).
-- Each column is dropped with IF EXISTS so the rollback is idempotent and
-- safe to re-run. The list mirrors exactly the ADD COLUMN statements in
-- 004_extend_gda_table_for_389_audit.sql lines 27-128.
-- ===========================================================================
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS pmid_list_was_capped;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS association_type_was_filled;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS disease_name_was_filled;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS score_direction;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS score_was_coerced_nan;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS original_score;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS score_was_clipped;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS source_url;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS snapshot_tag;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS schema_version;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS original_pmid_count;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS gene_to_uniprot_map_version;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS resolution_method;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS confidence_tier_method;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS dedup_strategy;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS source_format;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS download_method;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS download_date;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS source_version;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS normalized_score;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS evidence_strength;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS confidence_tier;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS year_final;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS year_initial;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS disease_class_source;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS disease_class;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS source_id;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS disease_type;
ALTER TABLE gene_disease_associations DROP COLUMN IF EXISTS gene_id;

-- ===========================================================================
-- Phase 5: do NOT touch the schema_version table — the row inserted by 004
-- (version=4) is left in place as an audit trail. Operators who want to
-- also remove the version marker can do so manually:
--   DELETE FROM schema_version WHERE version = 4;
-- ===========================================================================

COMMIT;
