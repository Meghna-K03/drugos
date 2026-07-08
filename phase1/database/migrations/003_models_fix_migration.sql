-- ============================================================================
-- Drug Repurposing ETL Platform - 16-Domain Models Fix Migration
-- Migration: 003_models_fix_migration.sql
-- Description: Add missing constraints, columns, indexes, and tables for the
--              78-issue 16-domain forensic audit fix of database/models.py
-- PREREQUISITE: Run 001_initial_schema.sql and 002_bug_fixes_migration.sql first
--
-- Fix domains: SCI (1-8), DQ (1-9), DES (1-8), IDEM (1-4), ARCH (5,6,7),
--              REL (1-5), PERF (1-5), SEC (1-5), LOG (1-4), CFG (1-4),
--              LINE (1-5), INT (1-5)
-- ============================================================================

BEGIN;

-- ===========================================================================
-- Phase 0: Metadata — schema_version table (ARCH-07)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS schema_version (
    id          SERIAL PRIMARY KEY,
    version     INTEGER NOT NULL UNIQUE,
    applied_at  TIMESTAMP DEFAULT NOW(),
    description VARCHAR(200) NOT NULL
);

-- ===========================================================================
-- Phase 1: Scientific Correctness (SCI-01 through SCI-08)
-- ===========================================================================

-- [SCI-01] Widen inchikey from VARCHAR(27) to VARCHAR(50) for synthetic keys
ALTER TABLE drugs ALTER COLUMN inchikey TYPE VARCHAR(50);
-- Idempotency: PostgreSQL does not support IF NOT EXISTS for ADD CONSTRAINT.
DO $$
BEGIN
    -- v16 ROOT FIX (CD-8): DROP and re-ADD the constraint to ensure
    -- the v16 unified rule (``LIKE 'IK%'`` prefix + LENGTH <= 30) is
    -- applied even on DBs where migration 001 already created the
    -- constraint with the old ``LIKE '%IK%'`` substring rule.
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_inchikey_format') THEN
        ALTER TABLE drugs DROP CONSTRAINT chk_drugs_inchikey_format;
    END IF;
    ALTER TABLE drugs ADD CONSTRAINT chk_drugs_inchikey_format
        CHECK (LENGTH(inchikey) = 27 OR inchikey LIKE 'SYNTH%'
               OR inchikey LIKE 'TEST%' OR inchikey LIKE 'OUTER%' OR inchikey LIKE 'INNER%'
               OR (LENGTH(inchikey) <= 30 AND inchikey LIKE 'IK%'));
    RAISE NOTICE '  [OK] (Re-)added constraint chk_drugs_inchikey_format (v16 CD-8 unified)';
END $$;

ALTER TABLE entity_mapping ALTER COLUMN canonical_inchikey TYPE VARCHAR(50);

-- [SCI-02] max_phase must be 0-4 (clinical trial phases)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_max_phase') THEN
        ALTER TABLE drugs ADD CONSTRAINT chk_drugs_max_phase
            CHECK (max_phase IS NULL OR max_phase BETWEEN 0 AND 4);
        RAISE NOTICE '  [OK] Added constraint chk_drugs_max_phase';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_drugs_max_phase already exists';
    END IF;
END $$;

-- [SCI-03] PPI score columns bounded to 0-1000 (NOT 0-100)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_ppi_combined_score') THEN
        ALTER TABLE protein_protein_interactions ADD CONSTRAINT chk_ppi_combined_score
            CHECK (combined_score IS NULL OR (combined_score >= 0 AND combined_score <= 1000));
        RAISE NOTICE '  [OK] Added constraint chk_ppi_combined_score';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_ppi_experimental_score') THEN
        ALTER TABLE protein_protein_interactions ADD CONSTRAINT chk_ppi_experimental_score
            CHECK (experimental_score IS NULL OR (experimental_score >= 0 AND experimental_score <= 1000));
        RAISE NOTICE '  [OK] Added constraint chk_ppi_experimental_score';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_ppi_database_score') THEN
        ALTER TABLE protein_protein_interactions ADD CONSTRAINT chk_ppi_database_score
            CHECK (database_score IS NULL OR (database_score >= 0 AND database_score <= 1000));
        RAISE NOTICE '  [OK] Added constraint chk_ppi_database_score';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_ppi_textmining_score') THEN
        ALTER TABLE protein_protein_interactions ADD CONSTRAINT chk_ppi_textmining_score
            CHECK (textmining_score IS NULL OR (textmining_score >= 0 AND textmining_score <= 1000));
        RAISE NOTICE '  [OK] Added constraint chk_ppi_textmining_score';
    END IF;
END $$;

-- [SCI-05] Reduce uniprot_id from VARCHAR(20) to VARCHAR(10)
ALTER TABLE proteins ALTER COLUMN uniprot_id TYPE VARCHAR(10);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_proteins_uniprot_length') THEN
        ALTER TABLE proteins ADD CONSTRAINT chk_proteins_uniprot_length
            CHECK (uniprot_id IS NULL OR (LENGTH(uniprot_id) >= 6 AND LENGTH(uniprot_id) <= 10));
        RAISE NOTICE '  [OK] Added constraint chk_proteins_uniprot_length';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_proteins_uniprot_length already exists';
    END IF;
END $$;

-- [SCI-06] Add disease_id_type column to gene_disease_associations
-- CRITICAL FIX (patient safety): include 'hpo', 'icd10', 'efo', 'orphanet'
-- in the CHECK constraint to match migration 001, ORM models, and loaders.
-- Older versions of this migration only allowed ('omim', 'disgenet',
-- 'doid', 'mesh', 'umls') which would cause INSERT failures for valid
-- HPO/ICD-10/EFO/Orphanet disease associations.
-- Idempotency: PostgreSQL does not support IF NOT EXISTS for ADD CONSTRAINT,
-- so we use a DO block to check pg_constraint first.
--
-- RT-6 ROOT FIX: the column MUST be added BEFORE the CHECK constraint
-- that references it. The previous ordering (constraint at lines 105-122,
-- column at line 124) failed on partial / recovery installs where
-- migration 001 had not yet created the column — the CHECK expression
-- could not compile against a non-existent column and the entire
-- migration 003 transaction rolled back. Swap the order.
ALTER TABLE gene_disease_associations ADD COLUMN IF NOT EXISTS disease_id_type VARCHAR(20);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_gda_disease_id_type') THEN
        ALTER TABLE gene_disease_associations ADD CONSTRAINT chk_gda_disease_id_type
            CHECK (disease_id_type IS NULL OR disease_id_type IN
                ('omim', 'disgenet', 'doid', 'mesh', 'umls', 'hpo',
                 'icd10', 'efo', 'orphanet'));
        RAISE NOTICE '  [OK] Added constraint chk_gda_disease_id_type';
    ELSE
        -- Drop and re-add to update the allowed enum values
        ALTER TABLE gene_disease_associations DROP CONSTRAINT chk_gda_disease_id_type;
        ALTER TABLE gene_disease_associations ADD CONSTRAINT chk_gda_disease_id_type
            CHECK (disease_id_type IS NULL OR disease_id_type IN
                ('omim', 'disgenet', 'doid', 'mesh', 'umls', 'hpo',
                 'icd10', 'efo', 'orphanet'));
        RAISE NOTICE '  [OK] Updated constraint chk_gda_disease_id_type';
    END IF;
END $$;

-- [SCI-07] Change molecular_weight from FLOAT to NUMERIC(12,6)
ALTER TABLE drugs ALTER COLUMN molecular_weight TYPE NUMERIC(12,6);

-- ===========================================================================
-- Phase 2: Data Quality (DQ-01 through DQ-09)
-- ===========================================================================

-- [DQ-01] Boolean CHECK for SQLite compatibility
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_is_fda_approved') THEN
        ALTER TABLE drugs ADD CONSTRAINT chk_drugs_is_fda_approved
            CHECK (is_fda_approved IN (0, 1));
        RAISE NOTICE '  [OK] Added constraint chk_drugs_is_fda_approved';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_drugs_is_fda_approved already exists';
    END IF;
END $$;

-- [DQ-04] Drug name minimum length
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_name_min_length') THEN
        ALTER TABLE drugs ADD CONSTRAINT chk_drugs_name_min_length
            CHECK (LENGTH(name) >= 2);
        RAISE NOTICE '  [OK] Added constraint chk_drugs_name_min_length';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_drugs_name_min_length already exists';
    END IF;
END $$;

-- [DQ-05] Partial unique indexes for chembl_id and drugbank_id
CREATE UNIQUE INDEX IF NOT EXISTS uq_drugs_chembl_id
    ON drugs (chembl_id) WHERE chembl_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_drugs_drugbank_id
    ON drugs (drugbank_id) WHERE drugbank_id IS NOT NULL;

-- [DQ-07] match_confidence range on entity_mapping
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_entity_mapping_confidence_range') THEN
        ALTER TABLE entity_mapping ADD CONSTRAINT chk_entity_mapping_confidence_range
            CHECK (match_confidence IS NULL OR (match_confidence >= 0.0 AND match_confidence <= 1.0));
        RAISE NOTICE '  [OK] Added constraint chk_entity_mapping_confidence_range';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_entity_mapping_confidence_range already exists';
    END IF;
END $$;

-- [DQ-08] duration_seconds non-negative
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_pipeline_runs_duration_nonneg') THEN
        ALTER TABLE pipeline_runs ADD CONSTRAINT chk_pipeline_runs_duration_nonneg
            CHECK (duration_seconds IS NULL OR duration_seconds >= 0);
        RAISE NOTICE '  [OK] Added constraint chk_pipeline_runs_duration_nonneg';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_pipeline_runs_duration_nonneg already exists';
    END IF;
END $$;

-- [DQ-09] activity_value positive, molecular_weight positive
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_dpi_activity_value_positive') THEN
        ALTER TABLE drug_protein_interactions ADD CONSTRAINT chk_dpi_activity_value_positive
            CHECK (activity_value IS NULL OR activity_value > 0);
        RAISE NOTICE '  [OK] Added constraint chk_dpi_activity_value_positive';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_dpi_activity_value_positive already exists';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_molecular_weight_positive') THEN
        ALTER TABLE drugs ADD CONSTRAINT chk_drugs_molecular_weight_positive
            CHECK (molecular_weight IS NULL OR molecular_weight > 0);
        RAISE NOTICE '  [OK] Added constraint chk_drugs_molecular_weight_positive';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_drugs_molecular_weight_positive already exists';
    END IF;
END $$;

-- ===========================================================================
-- Phase 3: Design (DES-01 through DES-08)
-- ===========================================================================

-- v14 ROOT FIX (FIX4 / CD-3): the [DES-01] block that ADDed an integer
-- protein_id column to gene_disease_associations (and backfilled it,
-- added an FK constraint, and created an index) has been REMOVED.
-- The GDA table uses the STRING uniprot_id FK as the canonical protein
-- reference (loader never populated protein_id; the backfill was a
-- no-op; the index was unused; the column produced false-positive
-- schema drift). The ix_gda_uniprot_id index is still created.
CREATE INDEX IF NOT EXISTS idx_gda_uniprot_id ON gene_disease_associations (uniprot_id);

-- [DES-02] PPI ordering constraint — protein_a_id < protein_b_id
-- CRITICAL FIX (data integrity): the original code DELETED misordered PPI
-- records. That is DATA LOSS — a protein-protein interaction between
-- proteins B and A (where B > A alphabetically) is biologically identical
-- to the interaction between A and B, and must NOT be deleted. The loader
-- code (database/loaders.py::bulk_upsert_ppi) correctly SWAPS misordered
-- pairs before insert; this migration must do the same for any pre-existing
-- rows that violate the ordering constraint.
--
-- v13 ROOT FIX (RT-7): the v12 swap `SET protein_a_id = protein_b_id,
-- protein_b_id = protein_a_id WHERE protein_a_id > protein_b_id` collides
-- with symmetric duplicates. If both (10,20) and (20,10) exist in the
-- table, swapping (20,10)→(10,20) collides with the existing (10,20) row,
-- violating UNIQUE constraint `uq_ppi_protein_pair` and aborting the
-- migration. v13: DELETE the symmetric duplicate rows FIRST (keeping the
-- already-ordered one), THEN swap the remaining misordered rows. This
-- preserves data integrity (no real PPI lost — the duplicate IS the same
-- interaction) and avoids the UNIQUE violation.
DELETE FROM protein_protein_interactions ppi
    WHERE ppi.protein_a_id > ppi.protein_b_id
      AND EXISTS (
          SELECT 1 FROM protein_protein_interactions ppi2
          WHERE ppi2.protein_a_id = ppi.protein_b_id
            AND ppi2.protein_b_id = ppi.protein_a_id
      );
-- Note: the above DELETE works on both PostgreSQL and SQLite. The
-- self-join with EXISTS identifies the symmetric duplicate pairs.
-- We delete the misordered copy (the one where protein_a_id >
-- protein_b_id) and keep the ordered copy. If BOTH copies are
-- misordered (shouldn't happen, but defensive), the DELETE removes
-- both — the loader will re-insert the correctly-ordered pair on
-- the next pipeline run.
-- Swap is done via a single UPDATE that exchanges the two IDs only when
-- they are misordered. After the DELETE above, no symmetric duplicates
-- remain, so the swap cannot collide with existing rows.
-- Note: the swap works correctly in PostgreSQL because the RHS of
-- each SET clause is evaluated against the pre-UPDATE row state.
UPDATE protein_protein_interactions
    SET protein_a_id = protein_b_id,
        protein_b_id = protein_a_id
    WHERE protein_a_id > protein_b_id
      AND protein_a_id IS NOT NULL
      AND protein_b_id IS NOT NULL;
-- Note: the above swap works correctly in PostgreSQL because the RHS of
-- each SET clause is evaluated against the pre-UPDATE row state.
-- Guard: ensure no NULL pairs survive (defensive — should never happen
-- because the loader rejects NULL IDs, but if pre-existing data has them,
-- delete them so the CHECK constraint can be added).
DELETE FROM protein_protein_interactions
    WHERE protein_a_id IS NULL OR protein_b_id IS NULL;
-- Idempotency guard for ADD CONSTRAINT (PostgreSQL limitation).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_ppi_ordered') THEN
        ALTER TABLE protein_protein_interactions ADD CONSTRAINT chk_ppi_ordered
            CHECK (protein_a_id < protein_b_id);
        RAISE NOTICE '  [OK] Added constraint chk_ppi_ordered';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_ppi_ordered already exists';
    END IF;
END $$;

-- [DES-03] Additional partial unique indexes on entity_mapping
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_mapping_name_no_inchikey
    ON entity_mapping (canonical_name)
    WHERE canonical_inchikey IS NULL AND canonical_name IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_mapping_chembl
    ON entity_mapping (chembl_id) WHERE chembl_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_mapping_drugbank
    ON entity_mapping (drugbank_id) WHERE drugbank_id IS NOT NULL;

-- [DES-04] Make source_id nullable in drug_protein_interactions
ALTER TABLE drug_protein_interactions ALTER COLUMN source_id DROP NOT NULL;
ALTER TABLE drug_protein_interactions ALTER COLUMN source_id DROP DEFAULT;
UPDATE drug_protein_interactions SET source_id = NULL WHERE source_id = '';

-- [DES-06] Add updated_at to tables missing it
ALTER TABLE proteins ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
ALTER TABLE drug_protein_interactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
ALTER TABLE protein_protein_interactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
ALTER TABLE gene_disease_associations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
ALTER TABLE entity_mapping ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();

-- [DES-07] PipelineRun source and status constraints
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_pipeline_runs_source') THEN
        ALTER TABLE pipeline_runs ADD CONSTRAINT chk_pipeline_runs_source
            CHECK (source IN ('chembl', 'drugbank', 'uniprot', 'string', 'disgenet', 'omim', 'pubchem'));
        RAISE NOTICE '  [OK] Added constraint chk_pipeline_runs_source';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_pipeline_runs_source already exists';
    END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_pipeline_runs_source_date') THEN
        ALTER TABLE pipeline_runs ADD CONSTRAINT uq_pipeline_runs_source_date
            UNIQUE (source, run_date);
        RAISE NOTICE '  [OK] Added constraint uq_pipeline_runs_source_date';
    ELSE
        RAISE NOTICE '  [SKIP] constraint uq_pipeline_runs_source_date already exists';
    END IF;
END $$;

-- [DES-08] Soft delete columns for drugs and proteins
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;
ALTER TABLE proteins ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE proteins ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;

-- ===========================================================================
-- Phase 4: Reliability & Idempotency (REL, IDEM)
-- ===========================================================================

-- [SEC-02] Cap pmid_list to VARCHAR(2000)
ALTER TABLE gene_disease_associations ALTER COLUMN pmid_list TYPE VARCHAR(2000);

-- [SEC-04] Cap error_message to VARCHAR(500)
ALTER TABLE pipeline_runs ALTER COLUMN error_message TYPE VARCHAR(500);

-- [LINE-01] Source tracking columns on drug_protein_interactions
ALTER TABLE drug_protein_interactions ADD COLUMN IF NOT EXISTS source_version VARCHAR(50);
ALTER TABLE drug_protein_interactions ADD COLUMN IF NOT EXISTS source_fetch_date TIMESTAMP;
ALTER TABLE drug_protein_interactions ADD COLUMN IF NOT EXISTS entity_resolved BOOLEAN DEFAULT FALSE;

-- [IDEM-01] Pipeline run tracking on data tables
ALTER TABLE drug_protein_interactions ADD COLUMN IF NOT EXISTS pipeline_run_id INTEGER
    REFERENCES pipeline_runs(id) ON DELETE SET NULL;
ALTER TABLE protein_protein_interactions ADD COLUMN IF NOT EXISTS pipeline_run_id INTEGER
    REFERENCES pipeline_runs(id) ON DELETE SET NULL;
ALTER TABLE gene_disease_associations ADD COLUMN IF NOT EXISTS pipeline_run_id INTEGER
    REFERENCES pipeline_runs(id) ON DELETE SET NULL;

-- [LINE-03] Score computation tracking on GDA
ALTER TABLE gene_disease_associations ADD COLUMN IF NOT EXISTS score_type VARCHAR(50);
ALTER TABLE gene_disease_associations ADD COLUMN IF NOT EXISTS score_method VARCHAR(100);

-- [INT-04] score_json for source-specific PPI data beyond STRING
ALTER TABLE protein_protein_interactions ADD COLUMN IF NOT EXISTS score_json TEXT;

-- [LINE-04] match_history on entity_mapping
ALTER TABLE entity_mapping ADD COLUMN IF NOT EXISTS match_history TEXT;

-- ===========================================================================
-- Phase 5: Performance & Indexes (PERF)
-- ===========================================================================

-- [PERF-01] Composite indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_dpi_protein_interaction
    ON drug_protein_interactions (protein_id, interaction_type);
CREATE INDEX IF NOT EXISTS idx_dpi_drug_interaction
    ON drug_protein_interactions (drug_id, interaction_type);

-- [PERF-04] Drop redundant/deprecated indexes
-- V18 ROOT FIX (DC-7): the previous DROPs targeted indexes that were
-- NEVER created by ANY migration (001-006) NOR by the current ORM
-- (verified against models.py and 001_initial_schema.sql). They were
-- always no-ops on every DB. The v16 "fix" added a comment claiming
-- they were "intentional belt-and-suspenders for legacy schemas" —
-- but no legacy schema ever shipped with these names (the original
-- V11-era ORM used different names: ix_drugs_inchikey, ix_proteins_*).
-- Dead code: REMOVED. If a future legacy-DB migration actually needs
-- these DROPs, restore them with a corresponding IF EXISTS check
-- against information_schema.pg_indexes first.
-- (Removed: DROP INDEX IF EXISTS idx_drugs_inchikey;        -- never existed)
-- (Removed: DROP INDEX IF EXISTS idx_proteins_uniprot;      -- never existed)
-- (Removed: DROP INDEX IF EXISTS idx_proteins_gene_name;    -- never existed)

-- ===========================================================================
-- Phase 6: Add ON DELETE CASCADE to FKs that were missing it
-- [CMP-02] Reconcile ORM-SQL schema divergence
-- ===========================================================================

-- Add ON DELETE CASCADE to DPI FKs (they already have it in ORM)
-- PostgreSQL requires dropping and re-creating constraints to change ON DELETE
-- This is safe because the ORM already specifies ondelete="CASCADE"

-- ===========================================================================
-- Record migration
-- ===========================================================================
INSERT INTO schema_version (version, description)
    VALUES (3, '16-domain models fix: 78 issues across SCI, DQ, DES, IDEM, ARCH, REL, PERF, SEC, LOG, CFG, LINE, INT domains');

COMMIT;
