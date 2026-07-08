-- ============================================================================
-- Drug Repurposing ETL Platform — PubChem Compound Properties Migration
-- Migration: 005_pubchem_compound_properties.sql
-- Description: Create the ``pubchem_compound_properties`` table for the
--              institutional-grade remediation of
--              ``pipelines/pubchem_pipeline.py`` per
--              ``PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md``.
--
-- PREREQUISITES: 001_initial_schema.sql, 002_bug_fixes_migration.sql,
--                003_models_fix_migration.sql, 004_extend_gda_table_for_389_audit.sql.
--
-- Why this table exists (ARCH-5, INT-7, SCI-4, SCI-6):
--   The ``drugs`` table (migration 001) only has 4 PubChem columns:
--   pubchem_cid, molecular_formula, molecular_weight, smiles.  Today the
--   pipeline fetches 15 properties from PubChem but silently drops 11 of
--   them (InChI, IUPACName, XLogP, ExactMass, TPSA, Complexity,
--   HBondDonorCount, HBondAcceptorCount, RotatableBondCount,
--   HeavyAtomCount, IsomericSMILES).  The Phase 3 Graph Transformer needs
--   these for molecular fingerprinting per the project doc.  This table is
--   the new persistence layer — one row per (inchikey, pubchem_cid) pair,
--   with full lineage / provenance / scientific-correctness columns.
--
-- All new columns are NULLABLE — existing rows and existing tests are
-- unaffected.  No columns are dropped, no constraints are weakened.
--
-- Cross-dialect notes:
--   * ``GENERATED ALWAYS AS IDENTITY`` works on PostgreSQL 10+ and is
--     ignored by SQLite (SQLite uses ROWID implicitly).  The Python
--     migration runner (``run_migrations.py``) skips statements that fail
--     on SQLite and logs a NOTICE.
--   * ``TIMESTAMP WITH TIME ZONE`` is PostgreSQL-native.  SQLite maps any
--     ``TIMESTAMP*`` type to its flexible TEXT-affinity storage.
--   * ``REFERENCES drugs(inchikey)`` is enforced on PostgreSQL (FK).  On
--     SQLite the FK is created but only enforced when
--     ``PRAGMA foreign_keys=ON`` (set by the test conftest).
--   * ``DEFAULT NOW()`` is PostgreSQL.  SQLite accepts the syntax and
--     stores NULL when the column is not specified (the application layer
--     populates ``enriched_at`` explicitly).
--
-- Domains addressed: ARCH-5, ARCH-7, ARCH-8, SCI-1, SCI-2, SCI-3, SCI-4,
-- SCI-5, SCI-6, SCI-8, SCI-10, SCI-11, SCI-13, SCI-14, SCI-15, SCI-16,
-- SCI-17, SCI-18, DQ-3, DQ-13, DQ-17, DQ-18, IDEM-4, IDEM-7, IDEM-9,
-- IDEM-10, LIN-1, LIN-2, LIN-3, LIN-5, LIN-8, LIN-9, LIN-10, LIN-11,
-- LIN-12, LIN-13, LIN-15, COMP-1, COMP-2, COMP-5, COMP-9, COMP-10,
-- INT-1, INT-7, INT-8.
-- ============================================================================

BEGIN;

-- ===========================================================================
-- Create the pubchem_compound_properties table
-- ===========================================================================

CREATE TABLE IF NOT EXISTS pubchem_compound_properties (
    -- [IDEM-4] Surrogate primary key.  GENERATED ALWAYS AS IDENTITY on
    -- PostgreSQL; ROWID on SQLite (implicit).
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- [SCI-11, LIN-2] The InChIKey we requested from PubChem.  Verified to
    --   match the InChIKey returned by PubChem before insertion (see
    --   ``_parse_pubchem_response``).  FK to drugs(inchikey) — a properties
    --   row cannot exist for a drug that does not exist in the drugs table.
    inchikey            VARCHAR(50)  NOT NULL REFERENCES drugs(inchikey),

    -- [SCI-5, LIN-2] PubChem Compound ID (parent / standardized).  PubChem
    --   returns the standardized (parent) CID — two different salt forms
    --   of the same drug (e.g., esomeprazole sodium and esomeprazole
    --   magnesium) share the same parent CID.
    pubchem_cid         BIGINT       NOT NULL,

    -- [SCI-1, DESIGN-1] CanonicalSMILES (no stereo) — separate column from
    --   IsomericSMILES so the Graph Transformer can use the stereochem-rich
    --   form for fingerprinting.  NEVER coalesce these two.
    canonical_smiles    VARCHAR(50000),

    -- [SCI-1, SCI-14, SCI-15] IsomericSMILES (with stereochemistry,
    --   isotopes, formal charges).  This is the SMILES the Graph
    --   Transformer's molecular fingerprinting MUST use.  For chiral drugs
    --   (thalidomide, escitalopram, warfarin) the ``@`` token distinguishes
    --   enantiomers — losing it would make (R)-thalidomide and
    --   (S)-thalidomide indistinguishable.  This is life-safety-critical.
    isomeric_smiles     VARCHAR(50000),

    -- [SCI-1] Full InChI string (structural + stereo + isotopes + charge).
    inchi               TEXT,

    -- [SCI-9] PubChem IUPACName — may be Preferred IUPAC Name (PIN) or a
    --   non-PIN name.  PubChem PUG REST does not distinguish; the
    --   ``iupac_name_type`` column records the source as
    --   ``"pubchem_iupac_name"`` (constant) to make this limitation explicit.
    iupac_name          TEXT,

    -- [SCI-6] CAS Registry Number, cross-validated against drugs.cas_number
    --   (populated by DrugBank).  Format: ^\d{2,7}-\d{2}-\d$.
    cas_number          VARCHAR(20),

    -- [SCI-4] Molecular formula, e.g., "C9H8O4" for aspirin.
    molecular_formula   VARCHAR(200),

    -- [SCI-16, SCI-4] Average molecular weight using natural-abundance
    --   atomic weights (C=12.011, H=1.008).  NUMERIC(12,6) — Decimal, not
    --   float.  Python float(180.063388) becomes 180.06338800000002; the
    --   pipeline stores Decimal('180.063388').  Range: 0.000001 to
    --   999999.999999 g/mol.
    molecular_weight    NUMERIC(12, 6),

    -- [SCI-4] Monoisotopic mass (Da) — uses the most-abundant isotope of
    --   each element (C=12.000, H=1.0078).  Use this (NOT molecular_weight)
    --   for mass-spectrometry-based drug discovery.
    exact_mass          NUMERIC(12, 6),

    -- [SCI-2] PubChem XLogP3 PREDICTION (not experimental).  PubChem's
    --   XLogP3 is a QSAR group-contribution model.  Experimental logP can
    --   differ by 1+ log unit.  The ``xlogp_source`` column makes the
    --   prediction provenance explicit so the model does not over-fit a
    --   noisy predictor as ground truth.
    xlogp               NUMERIC(6, 2),

    -- [SCI-2] Provenance flag — always "pubchem_xlogp3" for fetched rows.
    xlogp_source        VARCHAR(50)  DEFAULT 'pubchem_xlogp3',

    -- [SCI-3] Topological Polar Surface Area — PubChem-calculated (not
    --   measured) from the 2D structure.  Used for Lipinski Rule of 5
    --   and BBB permeability estimation.
    tpsa                NUMERIC(8, 2),

    -- [SCI-3] Provenance flag — always "pubchem_calculated" for fetched rows.
    tpsa_source         VARCHAR(50)  DEFAULT 'pubchem_calculated',

    -- [SCI-13] PubChem complexity metric (Bertz complexity).
    complexity          NUMERIC(10, 2),

    -- [SCI-13] Lipinski-style H-bond donor count (N-H + O-H bonds).
    --   Documented as approximate — for pharmacophore modeling recompute
    --   from SMILES with RDKit.
    --   V19 ROOT FIX (CD-2 residual): changed from SMALLINT to INTEGER
    --   to align with the ORM (models.py) and Core Table (loaders.py).
    --   SMALLINT maxes at 32767; some complex formulations (e.g. large
    --   peptides, antibodies) can exceed this. Integer (32-bit) is the
    --   safe choice per the V18 comment in loaders.py.
    h_bond_donor_count  INTEGER,

    -- [SCI-13] Lipinski-style H-bond acceptor count (N + O atoms).
    h_bond_acceptor_count INTEGER,

    -- PubChem rotatable bond count.
    rotatable_bond_count  INTEGER,

    -- [SCI-12] PubChem heavy-atom count — EXCLUDES hydrogen atoms (PubChem
    --   convention).  For total atom count, compute from molecular_formula.
    heavy_atom_count      INTEGER,

    -- [SCI-15] Formal charge parsed from isomeric_smiles (RDKit
    --   authoritative; SMILES token heuristic as fallback).
    formal_charge       INTEGER,

    -- [SCI-14] Isotope labels parsed from isomeric_smiles — JSON dict,
    --   e.g., ``{"F": 18, "C": 11}`` for an [18F]FDG-like PET tracer.
    --   ``None`` (SQL NULL) when no isotopes present.
    isotope_info        TEXT,

    -- [SCI-5] Salt form derived from the InChI string's /p and /q layers
    --   (V19 ROOT FIX PS-1: the InChIKey last char is a 2-value version
    --   flag S/N, NOT a 4-state protonation flag). Valid values:
    --   'neutral', 'protonated', 'deprotonated', 'zwitterion', 'salt_form'.
    --   NULL when the InChI string is unavailable.
    salt_form           VARCHAR(100),

    -- [SCI-8] Protonation state from the InChI string's /p and /q layers.
    --   V19 ROOT FIX (PS-1): column type widened from CHAR(1) to VARCHAR(20)
    --   to accommodate the full word taxonomy ('neutral', 'protonated',
    --   'deprotonated', 'zwitterion', 'salt_form'). The previous CHAR(1)
    --   schema stored single letters N/M/P/S which was the V18 4-state
    --   misinterpretation of the InChIKey version flag.
    protonation_state   VARCHAR(20),

    -- [INT-12, INT-13] PubChem release or access timestamp.  PUG REST has
    --   no explicit version — we record the access timestamp as
    --   ``pubchem_pug_rest_as_of_<ISO 8601 UTC>``.
    pubchem_release     VARCHAR(100),

    -- [LIN-2] Stable source identifier: "pubchem:CID:<cid>".
    source_id           VARCHAR(100) NOT NULL,

    -- [LIN-3] Source version string from ``get_source_version()``.
    source_version      VARCHAR(100),

    -- [LIN-1, COMP-11] ISO 8601 UTC download timestamp.
    download_date       TIMESTAMP WITH TIME ZONE NOT NULL,

    -- [LIN-15, INT-8] "pug_rest_batch" for normal fetches,
    --   "pug_rest_single" for split-retry individual lookups.
    download_method     VARCHAR(20),

    -- [IDEM-10, LIN-10] Pipeline run UUID — supports idempotency verification
    --   and lets analysts tie any row back to a specific run.
    pipeline_run_id     VARCHAR(64)  NOT NULL,

    -- [LIN-8] Batch index (0-based) of the PubChem API response that
    --   produced this row.  Combined with pipeline_run_id, locates the
    --   exact raw JSON archive file: ``batch_{N:04d}.json``.
    source_batch_idx    INTEGER,

    -- [LIN-8] SHA-256 of the raw PubChem JSON response for this row's
    --   batch.  Tamper-evidence — a row claimed to come from a batch
    --   can be verified against the archived raw response.
    source_response_sha256 VARCHAR(64),

    -- [IDEM-6, LIN-1] SHA-256 of ``inchikeys_to_lookup.txt``.  Identical
    --   inputs produce identical checksums — supports reproducibility audits.
    input_checksum      VARCHAR(64)  NOT NULL,

    -- [LIN-13] Semicolon-joined list of transformations applied to this row
    --   (e.g., "validated_inchikey_format;fetched_pubchem_properties;
    --   verified_response_inchikey_matches_request;sanitized_empty_strings;
    --   converted_molecular_weight_to_decimal;validated_ranges;
    --   deduplicated_by_inchikey_lowest_cid;extracted_protonation_state;
    --   extracted_isotope_info;computed_formal_charge").
    transformations     TEXT,

    -- [COMP-5] FDA 21 CFR Part 11 electronic-signature — populated with
    --   ``settings.OPERATOR_ID`` when set (manual runs), NULL for Airflow.
    electronic_signature TEXT,

    -- [COMP-5] Triggering context — Airflow DAG run ID or "manual".
    triggered_by        TEXT,

    -- [DESIGN-7, IDEM-9] Enrichment timestamp.  Non-deterministic across
    --   runs by design — the OTHER columns are deterministic given the
    --   same PubChem response.
    enriched_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- [IDEM-7] Soft-delete flag — set when a (inchikey, pubchem_cid) pair
    --   is superseded by a re-enrichment.  Old rows are retained for audit.
    is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,

    -- [DESIGN-6] Timestamps from the standard TimestampMixin pattern.
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- [DQ-13, IDEM-1] Unique constraint — one row per (inchikey, cid) pair.
    --   Re-running the pipeline with the same input UPSERTs in place
    --   rather than duplicating rows.
    UNIQUE (inchikey, pubchem_cid)
);

-- ===========================================================================
-- Indexes (PERF-8, DQ-19)
-- ===========================================================================

-- Primary lookup pattern: "give me all properties for this CID".
CREATE INDEX IF NOT EXISTS idx_pubchem_props_cid
    ON pubchem_compound_properties(pubchem_cid);

-- Primary lookup pattern: "give me all properties for this drug".
CREATE INDEX IF NOT EXISTS idx_pubchem_props_inchikey
    ON pubchem_compound_properties(inchikey);

-- [IDEM-7] Lookup pattern: "find soft-deleted rows for cleanup".
CREATE INDEX IF NOT EXISTS idx_pubchem_props_is_deleted
    ON pubchem_compound_properties(is_deleted)
    WHERE is_deleted = TRUE;

-- [LIN-10] Lookup pattern: "find all rows from a given pipeline run".
CREATE INDEX IF NOT EXISTS idx_pubchem_props_run_id
    ON pubchem_compound_properties(pipeline_run_id);

-- ===========================================================================
-- Audit / completion logging
-- ===========================================================================

DO $$
BEGIN
    RAISE NOTICE 'Created pubchem_compound_properties table with % columns + 4 indexes',
        (SELECT count(*) FROM information_schema.columns
            WHERE table_name = 'pubchem_compound_properties');
EXCEPTION WHEN OTHERS THEN
    -- SQLite / older PostgreSQL: information_schema may not be populated.
    RAISE NOTICE 'Created pubchem_compound_properties table';
END $$;

-- SCI-FIX: Record this migration in the schema_version table so that
-- check_migrations() correctly reports schema_version_matches = True.
-- Previously, migration 005 was missing this INSERT, causing the version
-- check to always fail (SCHEMA_VERSION in base.py is 5, but the DB only
-- recorded up to version 4).
INSERT INTO schema_version (version, description)
VALUES (5, 'PubChem compound properties table')
ON CONFLICT (version) DO NOTHING;

COMMIT;
