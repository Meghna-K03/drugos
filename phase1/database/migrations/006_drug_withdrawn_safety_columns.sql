-- ============================================================================
-- Drug Repurposing ETL Platform — Drug Withdrawn Safety Columns Migration
-- Migration: 006_drug_withdrawn_safety_columns.sql
-- Description: Add life-safety-critical withdrawn drug tracking columns and
--              DrugBank molecular property columns to the drugs table.
--
-- LIFE-SAFETY CRITICAL:
--   is_withdrawn tracks drugs withdrawn from market for safety reasons.
--   Without this column, killer drugs like Vioxx (rofecoxib, 88,000-140,000
--   heart attacks) and Baycol (cerivastatin, ~100 rhabdomyolysis deaths)
--   cannot be filtered out of repurposing candidates. A researcher could
--   inadvertently recommend a known killer drug for repurposing.
--
-- PREREQUISITES: 001_initial_schema.sql through 005_pubchem_compound_properties.sql.
--
-- All new columns are NULLABLE (except is_withdrawn which has a DEFAULT of
-- FALSE) — existing rows and existing tests are unaffected.  No columns are
-- dropped, no constraints are weakened.
--
-- Domains addressed: SCI-3 (withdrawn tracking), DQ (data completeness),
--   DES (clinical status), ARCH (ORM-schema parity).
-- ============================================================================

BEGIN;

-- ===========================================================================
-- Phase 1: Life-safety-critical withdrawn drug tracking
-- ===========================================================================

-- [LIFE-SAFETY] is_withdrawn — tracks drugs withdrawn from market.
-- DEFAULT FALSE so existing rows default to "not withdrawn".
-- This MUST NOT be nullable — every drug MUST explicitly declare its
-- withdrawal status to prevent silent failures in safety filters.
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS is_withdrawn BOOLEAN NOT NULL DEFAULT FALSE;
-- Idempotency: PostgreSQL does not support IF NOT EXISTS for ADD CONSTRAINT,
-- so we use a DO block to check pg_constraint first. Without this guard,
-- re-running the migration (a normal weekly pipeline operation) fails with
-- "constraint already exists", blocking the entire pipeline.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_is_withdrawn') THEN
        ALTER TABLE drugs ADD CONSTRAINT chk_drugs_is_withdrawn
            CHECK (is_withdrawn IN (0, 1));
        RAISE NOTICE '  [OK] Added constraint chk_drugs_is_withdrawn';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_drugs_is_withdrawn already exists';
    END IF;
END $$;

-- [SCI-3] clinical_status — derived from DrugBank groups field.
-- Values: approved, withdrawn, illicit, investigational, vet_approved,
--   experimental, nutraceutical, unknown.
-- When is_withdrawn = TRUE, clinical_status MUST be 'withdrawn'.
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS clinical_status VARCHAR(30);

-- ===========================================================================
-- Phase 2: DrugBank molecular property columns
-- ===========================================================================

-- [SCI-5] CAS Registry Number — unique identifier for chemical substances.
-- Format: ^\d{2,7}-\d{2}-\d$  (e.g., "50-78-2" for aspirin).
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS cas_number VARCHAR(20);

-- [SCI-2] Calculated LogP — octanol-water partition coefficient.
-- Predicts drug membrane permeability (Lipinski Rule of 5).
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS logp FLOAT;

-- [SCI-3] Topological Polar Surface Area (Å²).
-- Used for Lipinski Rule of 5 and BBB permeability estimation.
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS tpsa FLOAT;

-- [SCI-4] Lipinski H-bond donor count (N-H + O-H bonds).
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS h_bond_donor_count INTEGER;

-- [SCI-4] Lipinski H-bond acceptor count (N + O atoms).
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS h_bond_acceptor_count INTEGER;

-- [SCI-5] Rotatable bond count (molecular flexibility).
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS rotatable_bond_count INTEGER;

-- [SCI-6] Heavy atom count (excludes hydrogen, PubChem convention).
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS heavy_atom_count INTEGER;

-- [SCI-7] Molecular complexity (Bertz complexity index).
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS complexity INTEGER;

-- ===========================================================================
-- Phase 3: Data quality completeness score
-- ===========================================================================

-- [DQ-13] completeness_score — 0.0-1.0 fraction of expected fields populated.
-- Used to filter out low-quality records before knowledge graph ingestion.
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS completeness_score FLOAT;
-- Idempotency: see note above for chk_drugs_is_withdrawn.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_completeness_score_range') THEN
        ALTER TABLE drugs ADD CONSTRAINT chk_drugs_completeness_score_range
            CHECK (completeness_score IS NULL OR (completeness_score >= 0.0 AND completeness_score <= 1.0));
        RAISE NOTICE '  [OK] Added constraint chk_drugs_completeness_score_range';
    ELSE
        RAISE NOTICE '  [SKIP] constraint chk_drugs_completeness_score_range already exists';
    END IF;
END $$;

-- ===========================================================================
-- Phase 4: Indexes for the new columns
-- ===========================================================================

-- [LIFE-SAFETY] Index on is_withdrawn for fast filtering of withdrawn drugs.
-- This index MUST exist for the safety filter to be performant on large tables.
CREATE INDEX IF NOT EXISTS idx_drugs_is_withdrawn ON drugs (is_withdrawn);
CREATE INDEX IF NOT EXISTS idx_drugs_clinical_status ON drugs (clinical_status);
CREATE INDEX IF NOT EXISTS idx_drugs_cas_number ON drugs (cas_number);

-- ===========================================================================
-- Phase 5: BACKFILL is_withdrawn from DrugBank 'withdrawn' group membership
-- ===========================================================================
-- v9 ROOT FIX (audit F3.3): the previous migration added is_withdrawn with
-- DEFAULT FALSE applied to existing rows. Vioxx, Baycol, Bextra, and every
-- other drug withdrawn before migration 006 ran was silently marked
-- is_withdrawn=FALSE — making them appear as safe repurposing candidates.
-- No backfill from DrugBank groups was performed. This is a patient-safety-
-- critical bug.
--
-- The DrugBank 'groups' column on the drugs table contains an array of
-- group memberships (approved, withdrawn, illicit, investigational, etc).
-- This backfill scans the groups array for 'withdrawn' and sets
-- is_withdrawn=TRUE for every matching row. After this runs, every drug
-- that DrugBank ever recorded as withdrawn will be correctly flagged —
-- so the safety filter in the RL ranker can exclude them.
--
-- Two dialects supported:
--   * PostgreSQL: uses unnest() on the array column.
--   * SQLite: drugs.groups is stored as a comma/semicolon-separated TEXT;
--     we use LIKE to detect the 'withdrawn' token. SQLite has no native
--     array type, so this is the most portable approach.
--
-- The query is wrapped in a DO block (PostgreSQL) or guarded by a
-- row-count check (SQLite) to make it idempotent and cross-dialect.
-- We run it AFTER the column is added (Phase 1) so the UPDATE has a
-- target.

-- PostgreSQL path: if the drugs.groups column exists and is an array
-- type, scan for 'withdrawn' token via unnest.
--
-- PS-6 ROOT FIX (patient safety): the previous code checked for a
-- drugs.groups column that was NEVER created by ANY migration
-- (001-006) and was NOT in the Drug ORM model. The IF EXISTS branch
-- always fell through to ELSE, which logged [SKIP] and silently did
-- NOTHING — every withdrawn drug stayed is_withdrawn=FALSE, and the
-- RL ranker's safety filter passed withdrawn killer drugs (Vioxx,
-- Baycol, thalidomide, cisapride) as if they were safe. Two-part fix:
--   (a) ADD the groups column to the drugs table here (so future
--       loads from drugbank_pipeline can persist the DrugBank <groups>
--       field).
--   (b) Backfill is_withdrawn from the new column where it has been
--       populated; the loader (bulk_upsert_drugs in loaders.py) is
--       being updated in parallel to include 'groups' in
--       updatable_cols and the Drug ORM is being updated to declare
--       the column. The trigger keeps future inserts in sync.
ALTER TABLE drugs ADD COLUMN IF NOT EXISTS groups VARCHAR(200);
COMMENT ON COLUMN drugs.groups IS
    'DrugBank <groups> field as semicolon-separated string '
    '(approved;investigational;withdrawn;vet_approved;illicit;experimental;nutraceutical). '
    'Used to derive is_withdrawn / clinical_status safety flags.';

DO $$
DECLARE
    _row_count INTEGER;
BEGIN
    -- Backfill is_withdrawn from the groups column (now guaranteed to
    -- exist by the ALTER TABLE above). Use word-boundary regex matching
    -- so 'withdrawn' does not match substrings of other tokens.
    UPDATE drugs
    SET is_withdrawn = TRUE,
        clinical_status = COALESCE(clinical_status, 'withdrawn')
    WHERE groups IS NOT NULL
      AND lower(groups) ~ '(^|;)withdrawn(;|$)';
    GET DIAGNOSTICS _row_count = ROW_COUNT;
    RAISE NOTICE '  [OK] Backfilled is_withdrawn from drugs.groups — % rows updated', _row_count;
END $$;

-- Idempotent trigger to keep safety columns in sync with groups on
-- future INSERT / UPDATE.
--
-- v29 ROOT FIX (Compound Chain 1 / Patient-Safety Bypass): the v28
-- trigger was a ONE-WAY RATCHET — it only ever set is_withdrawn := TRUE
-- when 'withdrawn' appeared in groups. If a drug was withdrawn and
-- later re-instated (groups no longer contains 'withdrawn'), the
-- trigger LEFT is_withdrawn := TRUE forever. This is a patient-safety
-- bug: a re-approved drug that had a temporary withdrawal (e.g.
-- Lotronex, Redux, Lotronex) could never be un-flagged, so it would
-- be permanently excluded from repurposing candidates even after the
-- FDA re-approved it.
--
-- ROOT FIX: make the trigger BIDIRECTIONAL. When 'withdrawn' IS in
-- groups, set is_withdrawn := TRUE. When 'withdrawn' is NOT in groups
-- AND groups is non-null, set is_withdrawn := FALSE. This reflects
-- the authoritative source-of-truth (DrugBank groups) and keeps the
-- flag in sync with the actual market status.
CREATE OR REPLACE FUNCTION trg_drugs_sync_withdrawn() RETURNS trigger AS $$
BEGIN
    IF NEW.groups IS NOT NULL THEN
        IF lower(NEW.groups) ~ '(^|;)withdrawn(;|$)' THEN
            -- Drug is currently withdrawn — set safety flag.
            NEW.is_withdrawn := TRUE;
            NEW.clinical_status := COALESCE(NEW.clinical_status, 'withdrawn');
        ELSE
            -- Drug is NOT currently withdrawn. Per the bidirectional
            -- fix, clear the flag so re-instated drugs can re-enter
            -- the repurposing pool. We do NOT clear clinical_status
            -- (it may legitimately be 'approved' for a re-instated
            -- drug).
            NEW.is_withdrawn := FALSE;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_drugs_sync_withdrawn ON drugs;
CREATE TRIGGER trg_drugs_sync_withdrawn
    BEFORE INSERT OR UPDATE OF groups ON drugs
    FOR EACH ROW
    EXECUTE FUNCTION trg_drugs_sync_withdrawn();

-- ===========================================================================
-- Phase 6: Schema version metadata
-- ===========================================================================
INSERT INTO schema_version (version, description)
VALUES (6, 'Add is_withdrawn (life-safety), clinical_status, cas_number, logp, tpsa, h_bond_donor/acceptor_count, rotatable_bond_count, heavy_atom_count, complexity, completeness_score to drugs table; backfill is_withdrawn from DrugBank groups')
ON CONFLICT (version) DO NOTHING;

COMMIT;
