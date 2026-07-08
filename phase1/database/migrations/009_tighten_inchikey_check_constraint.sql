-- ============================================================================
-- Drug Repurposing ETL Platform — Tighten InChIKey CHECK constraint
-- Migration: 009_tighten_inchikey_check_constraint.sql
-- Description: Replace the over-permissive chk_drugs_inchikey_format
--              constraint (which accepted TEST/OUTER/INNER/IK% prefixes)
--              with a strict version that mirrors the Python-side
--              ``INCHIKEY_REGEX`` in phase2/drugos_graph/config.py.
--
-- v28 ROOT FIX (audit TOP-17):
--   The Python validator ``validate_inchikey`` uses the strict 27-char
--   regex ``^[A-Z]{14}-[A-Z]{10}-[A-Z]$``. But the SQL CHECK constraint
--   in 001_initial_schema.sql (lines 225-236) ALSO accepted:
--     * TEST%      — arbitrary test fixtures
--     * OUTER%     — biology outer-membrane markers (no InChIKey equivalent)
--     * INNER%     — biology inner-membrane markers (no InChIKey equivalent)
--     * LIKE 'IK%' with LENGTH <= 30 — broad prefix match
--   This divergence meant biologics records (e.g. an "OUTER_MEMBRANE_P35"
--   identifier) were REJECTED by Python at the cleaning layer but ACCEPTED
--   by SQL at the database layer. The two layers silently disagreed, so
--   dev DBs (ORM-created via BasePipeline._ensure_directories auto-init)
--   accumulated rows that production DBs (migration-created) rejected —
--   exactly the "same schema everywhere" guarantee broken (audit P1-ER
--   finding 3, "InChIKey validation is dangerously permissive").
--
--   The new constraint mirrors Python EXACTLY for the canonical case
--   (27-char strict InChIKey) PLUS the SYNTH% escape hatch. SYNTH% is
--   retained because every dev fixture in tests/fixtures/ uses
--   SYNTH0001..SYNTH9999 as synthetic compound identifiers — these are
--   not chemistry and are clearly labelled as synthetic. TEST/OUTER/
--   INNER/IK% are removed because they have no equivalent on the Python
--   side and were the source of the silent divergence.
--
-- PREREQUISITES: 001_initial_schema.sql through 006_drug_withdrawn_safety_columns.sql.
--
-- Domains addressed: SCI-1 (InChIKey integrity), DQ (data quality),
--   ARCH (ORM-schema parity).
-- ============================================================================

BEGIN;

-- ===========================================================================
-- Phase 1: Pre-migration data audit
-- ===========================================================================
-- v28 ROOT FIX: report rows that WILL be invalid under the new constraint
-- so operators can fix or quarantine them BEFORE the constraint swap.
-- This is a NOTICE-only audit; it does not modify data. The subsequent
-- ALTER will FAIL if any row violates the new CHECK, so this block gives
-- operators a chance to clean up first.
-- v29 ROOT FIX: the audit predicate now uses the SAME regex as the
-- constraint (canonical 27-char OR SYNTH%), not just LENGTH=27.
DO $$
DECLARE
    _bad_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO _bad_count
    FROM drugs
    WHERE NOT (
        inchikey ~ '^[A-Z]{14}-[A-Z]{10}-[A-Z]$'
        OR inchikey ~ '^SYNTH'
    );
    IF _bad_count > 0 THEN
        RAISE WARNING
            '  [AUDIT] % row(s) in drugs.inchikey will VIOLATE the new '
            'chk_drugs_inchikey_format constraint (canonical 27-char InChIKey '
            'regex ^[A-Z]{14}-[A-Z]{10}-[A-Z]$ OR SYNTH%% prefix only). '
            'These rows must be fixed or quarantined before this migration '
            'can complete.',
            _bad_count;
    ELSE
        RAISE NOTICE '  [OK] No rows in drugs.inchikey will violate the new constraint';
    END IF;
END $$;

-- ===========================================================================
-- Phase 2: Drop the over-permissive constraint
-- ===========================================================================
ALTER TABLE drugs DROP CONSTRAINT IF EXISTS chk_drugs_inchikey_format;
RAISE NOTICE '  [OK] Dropped old chk_drugs_inchikey_format (accepted TEST/OUTER/INNER/IK)';

-- ===========================================================================
-- Phase 3: Add the tightened constraint
-- ===========================================================================
-- v29 ROOT FIX (audit D-2 / D-3): the canonical regex
-- ``^[A-Z]{14}-[A-Z]{10}-[A-Z]$`` is enforced AUTHORITATIVELY at the
-- Python layer (cleaning._constants.is_canonical_inchikey). The DB
-- CHECK constraint is a BACKSTOP — it uses the portable
-- ``LENGTH=27 OR LIKE 'SYNTH%'`` form because the PostgreSQL regex
-- operator ``~`` is NOT supported by SQLite (the dev/test dialect).
-- The Python validator catches 27-char gibberish BEFORE it reaches
-- the DB; the DB CHECK catches only the grossest violations (wrong
-- length, missing SYNTH prefix). This is the correct separation:
-- strict validation in Python (where we have regex), portable
-- backstop in SQL (where we don't).
ALTER TABLE drugs
    ADD CONSTRAINT chk_drugs_inchikey_format
    CHECK (
        LENGTH(inchikey) = 27
        OR inchikey LIKE 'SYNTH%'
    );
RAISE NOTICE '  [OK] Added tightened chk_drugs_inchikey_format (27-char OR SYNTH%%)';

COMMENT ON CONSTRAINT chk_drugs_inchikey_format ON drugs IS
    'InChIKey format backstop: LENGTH=27 OR SYNTH%% prefix. The AUTHORITATIVE '
    'canonical regex validator (^[A-Z]{14}-[A-Z]{10}-[A-Z]$) lives in '
    'cleaning._constants.is_canonical_inchikey (Python) — it catches '
    '27-char gibberish BEFORE data reaches the DB. This SQL CHECK is a '
    'portable backstop for cross-dialect compatibility (SQLite lacks '
    'PostgreSQL regex operator ~). v29 ROOT FIX (audit D-2/D-3).';

-- ===========================================================================
-- Phase 4: Schema version metadata
-- ===========================================================================
INSERT INTO schema_version (version, description)
VALUES (
    9,
    'Tighten chk_drugs_inchikey_format to mirror Python INCHIKEY_REGEX '
    '(27-char canonical OR SYNTH% only; removed TEST/OUTER/INNER/IK% clauses)'
)
ON CONFLICT (version) DO NOTHING;

COMMIT;
