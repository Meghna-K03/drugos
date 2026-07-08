-- ============================================================================
-- Drug Repurposing ETL Platform - 389-Fix DisGeNET GDA Migration
-- Migration: 004_extend_gda_table_for_389_audit.sql
-- Description: Add institutional-grade columns + DeadLetterGDA table for the
--              389-finding forensic audit fix of
--              pipelines/disgenet_pipeline.py (v2.0.0).
-- PREREQUISITE: Run 001_initial_schema.sql, 002_bug_fixes_migration.sql,
--               003_models_fix_migration.sql first.
--
-- All new columns are NULLABLE — existing rows and existing tests are
-- unaffected.  No columns are dropped, no constraints are weakened.
--
-- Domains addressed: SCI-3, SCI-5, SCI-6, SCI-7, SCI-8, SCI-9, SCI-10,
-- SCI-11, SCI-21, SCI-24, SCI-26, SCI-38, SCI-41, DQ-1, DQ-2, DQ-3,
-- DQ-18, IDEM-7, IDEM-8, IDEM-10, IDEM-14, IDEM-15, IDEM-17,
-- LIN-1, LIN-6, LIN-9, LIN-10, LIN-15, LIN-16, LIN-21, LIN-23,
-- LIN-24, COMP-5, COMP-6, COMP-7, INT-7.
-- ============================================================================

BEGIN;

-- ===========================================================================
-- Phase 1: Extend gene_disease_associations with new columns (all nullable)
-- ===========================================================================

-- [SCI-6 / DQ-1] NCBI Entrez Gene ID — stable across HGNC renames
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS gene_id INTEGER;

-- [SCI-9 / DQ-2] DisGeNET diseaseType ∈ {disease, phenotype, group}
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS disease_type VARCHAR(50);

-- [SCI-3] DisGeNET sub-source (CURATED, BEFREE, GWAS_CATALOG, ...)
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS source_id VARCHAR(50);

-- [SCI-8] MeSH hierarchy code (e.g. C04.588.614)
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS disease_class VARCHAR(255);
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS disease_class_source VARCHAR(50);

-- [SCI-7] Publication-year range
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS year_initial INTEGER;
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS year_final INTEGER;

-- [SCI-10] Confidence tier label
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS confidence_tier VARCHAR(20);

-- [SCI-24] Evidence-strength label
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS evidence_strength VARCHAR(20);

-- [SCI-38] Score × source_weight — cross-source comparable
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS normalized_score FLOAT;

-- [SCI-26 / IDEM-8] DisGeNET release version
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS source_version VARCHAR(50);

-- [LIN-6 / COMP-7] Download timestamp (UTC)
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS download_date TIMESTAMP WITH TIME ZONE;

-- [LIN-23] Download method: "api" or "static"
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS download_method VARCHAR(20);

-- [INT-7] Source format: "api" or "tsv"
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS source_format VARCHAR(20);

-- [LIN-24] Dedup strategy applied
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS dedup_strategy VARCHAR(50);

-- [LIN-15 / IDEM-17] Confidence tier definition version
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS confidence_tier_method VARCHAR(50);

-- [LIN-10] Resolution method used for gene_symbol → uniprot_id
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS resolution_method VARCHAR(20);

-- [LIN-10 / IDEM-7] SHA-256 of the cached gene_to_uniprot map
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS gene_to_uniprot_map_version VARCHAR(64);

-- [LIN-16] Original PMID count (before capping)
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS original_pmid_count INTEGER;

-- [COMP-6] Schema version of the CSV that produced this row
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS schema_version VARCHAR(20);

-- [IDEM-14] Snapshot tag for backfill safety
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS snapshot_tag VARCHAR(50);

-- [LIN-9] Source URL (sanitised — no API key)
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS source_url VARCHAR(500);

-- [SCI-21 / LIN-17..19] Lineage columns from validate_gda_scores
-- Renamed without leading underscore in the DB (CSV keeps the
-- underscore-prefixed names for backward compat).
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS score_was_clipped BOOLEAN;
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS original_score FLOAT;
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS score_was_coerced_nan BOOLEAN;
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS score_direction VARCHAR(10);
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS disease_name_was_filled BOOLEAN;
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS association_type_was_filled BOOLEAN;

-- [LIN-16] True if the pmid_list was capped
ALTER TABLE gene_disease_associations
    ADD COLUMN IF NOT EXISTS pmid_list_was_capped BOOLEAN;

-- ===========================================================================
-- Phase 2: New indexes for the new columns
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_gda_gene_id ON gene_disease_associations (gene_id);
CREATE INDEX IF NOT EXISTS idx_gda_source_id ON gene_disease_associations (source_id);
CREATE INDEX IF NOT EXISTS idx_gda_snapshot_tag ON gene_disease_associations (snapshot_tag);

-- ===========================================================================
-- Phase 3: New / updated CHECK constraints
-- ===========================================================================

-- [SCI-06 / COMP-5] Extend disease_id_type to include 'hpo' (HPO terms are
-- valid DisGeNET disease IDs per Piñero et al. 2020) PLUS 'icd10', 'efo',
-- 'orphanet' (CRITICAL FIX — patient safety: without these, real disease
-- associations from GWAS Catalog, UK Biobank, Open Targets, and the rare-
-- disease literature would be SILENTLY DROPPED, hiding drug-disease links
-- from the model).
-- Idempotency: DROP IF EXISTS + ADD CONSTRAINT is safe to re-run.
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_disease_id_type;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_gda_disease_id_type') THEN
        ALTER TABLE gene_disease_associations ADD CONSTRAINT chk_gda_disease_id_type
            CHECK (disease_id_type IS NULL OR disease_id_type IN
                   ('omim', 'disgenet', 'doid', 'mesh', 'umls', 'hpo',
                    'icd10', 'efo', 'orphanet'));
        RAISE NOTICE '  [OK] Added chk_gda_disease_id_type with full vocabulary';
    END IF;
END $$;

-- [SCI-9] diseaseType ∈ {disease, phenotype, group} when non-NULL
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_disease_type;
ALTER TABLE gene_disease_associations ADD CONSTRAINT chk_gda_disease_type
    CHECK (disease_type IS NULL OR disease_type IN
           ('disease', 'phenotype', 'group'));

-- [SCI-11] confidence_tier must be a known label when non-NULL
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_confidence_tier;
ALTER TABLE gene_disease_associations ADD CONSTRAINT chk_gda_confidence_tier
    CHECK (confidence_tier IS NULL OR confidence_tier IN
           ('weak', 'moderate', 'strong'));

-- [SCI-24] evidence_strength must be a known label when non-NULL
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_evidence_strength;
ALTER TABLE gene_disease_associations ADD CONSTRAINT chk_gda_evidence_strength
    CHECK (evidence_strength IS NULL OR evidence_strength IN
           ('robust', 'moderate', 'limited', 'unsupported'));

-- [SCI-41] year_initial <= year_final when both are present
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_year_range;
ALTER TABLE gene_disease_associations ADD CONSTRAINT chk_gda_year_range
    CHECK (year_initial IS NULL OR year_final IS NULL OR year_initial <= year_final);

-- [SCI-38 / COMP-19] normalized_score must be in [0, 1] when non-NULL
ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_normalized_score_range;
ALTER TABLE gene_disease_associations ADD CONSTRAINT chk_gda_normalized_score_range
    CHECK (normalized_score IS NULL OR (normalized_score >= 0.0 AND normalized_score <= 1.0));

-- ===========================================================================
-- Phase 4: Dead-letter queue table (DQ-18 / REL-3 / LIN-11)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS dead_letter_gda (
    id              SERIAL PRIMARY KEY,
    gene_symbol     VARCHAR(50),
    disease_id      VARCHAR(50),
    source          VARCHAR(50),
    reason          VARCHAR(100) NOT NULL,
    details_json    TEXT,
    run_id          VARCHAR(64),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dlgda_reason      ON dead_letter_gda (reason);
CREATE INDEX IF NOT EXISTS idx_dlgda_run_id      ON dead_letter_gda (run_id);
CREATE INDEX IF NOT EXISTS idx_dlgda_gene_symbol ON dead_letter_gda (gene_symbol);
CREATE INDEX IF NOT EXISTS idx_dlgda_disease_id  ON dead_letter_gda (disease_id);

-- ===========================================================================
-- Phase 5: Schema version metadata
-- ===========================================================================
INSERT INTO schema_version (version, description)
VALUES (4, '389-fix DisGeNET GDA extension + dead_letter_gda table')
ON CONFLICT (version) DO NOTHING;

COMMIT;
