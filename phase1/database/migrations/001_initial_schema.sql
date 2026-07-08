-- ============================================================================
-- Drug Repurposing ETL Platform — Initial Schema
-- Migration: 001_initial_schema.sql
-- Description: Core staging tables for drug repurposing data pipeline
--
-- DIALECT: PostgreSQL 15+ (primary)
-- NOTE: Trigger functions, CHECK constraints, and advanced features
--       require PostgreSQL. SQLite fallbacks are handled by
--       database/migrations/run_migrations.py which translates
--       PostgreSQL SQL to SQLite-compatible operations.
--
-- DATA CLASSIFICATION:
--   Public:     drug names, protein sequences, interaction scores (public databases)
--   Sensitive:  gene-disease associations for rare diseases (potential patient identification)
--   Internal:   pipeline run metadata, error messages (may contain system details)
--
-- COMPLIANCE:
--   GDPR:  Gene symbols are NOT PII (Recital 26). All data sourced from
--          public databases. No patient-level data stored.
--   HIPAA: No Protected Health Information (PHI) stored. All data is
--          population-level, not patient-level.
--
-- RETENTION:
--   pipeline_runs:    2 years, then archived
--   rejected_records: 1 year, then purged
--   audit_log:        5 years for compliance
--   data tables:      indefinite (re-loaded weekly by Airflow pipelines)
--
-- ORM PARITY:
--   This schema must match the SQLAlchemy ORM models in database/models.py.
--   The ORM uses IDMixin (autoincrement), TimestampMixin (created_at,
--   updated_at), and SoftDeleteMixin (is_deleted, deleted_at).
--   Constraint names follow the naming convention in database/base.py.
--
-- CROSS-MIGRATION NOTES:
--   - Migration 002 (bug_fixes) adds gene_symbol, protein_name, function_desc
--     to proteins and deduplicates GDA/entity_mapping. Those columns are NOW
--     included here so 002 becomes a no-op for those additions.
--   - Migration 003 (models_fix) patches many of these same issues via ALTER
--     TABLE. With 001 fixed at the source, 003's ALTERs will be idempotent
--     no-ops (IF NOT EXISTS / IF EXISTS guards).
--   - The schema_version table is created here (ARCH-07). Migrations 003
--     also creates it with IF NOT EXISTS, making it safe.
--
-- PostgreSQL Version: 15+
-- SQLAlchemy Version: 2.0+
-- Last Validated: 2025-06-16
-- ============================================================================

BEGIN;

-- [ARCH-06, CFG-01] Explicit search_path prevents search_path injection
SET search_path TO public;

-- =====================================================================
-- METADATA: schema_version table (ARCH-02, ARCH-07)
-- WHY: Tracks which migration versions have been applied, enabling
--   the Python migration runner (run_migrations.py) to skip already-
--   applied migrations and detect schema-ORM drift at runtime.
-- =====================================================================

DO $$
BEGIN
    RAISE NOTICE 'Creating schema_version table...';
END $$;

CREATE TABLE IF NOT EXISTS schema_version (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version         INTEGER NOT NULL UNIQUE,
    applied_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    description     VARCHAR(200) NOT NULL
);

COMMENT ON TABLE schema_version IS
    'Tracks applied schema migration versions. One row per migration. '
    'The latest row indicates the current schema version. '
    'SCHEMA_VERSION in database/base.py must match the latest version here.';

COMMENT ON COLUMN schema_version.id IS
    'Auto-incrementing identity primary key (CMP-01: GENERATED ALWAYS AS IDENTITY).';
COMMENT ON COLUMN schema_version.version IS
    'Migration version number. Must be unique and sequential (1, 2, 3, ...).';
COMMENT ON COLUMN schema_version.applied_at IS
    'Timestamp when this migration was applied (server time, timezone-aware).';
COMMENT ON COLUMN schema_version.description IS
    'Human-readable description of what this migration does. Max 200 chars.';

-- Record this migration
INSERT INTO schema_version (version, description)
    VALUES (1, 'Initial schema: 7 core tables + schema_version + rejected_records + audit_log')
    ON CONFLICT (version) DO NOTHING;

-- =====================================================================
-- TRIGGER FUNCTION: update_updated_at() (ARCH-01, REL-02, IDEM-01)
-- WHY: PostgreSQL triggers are the only reliable way to auto-update
--   updated_at on every row modification, including bulk operations
--   where SQLAlchemy onupdate does NOT fire (IDEM-02).
-- WHY generic: One function shared by all tables instead of per-table
--   functions (ARCH-01). Per-table functions were a maintenance burden
--   and created unnecessary database objects.
-- NOTE: Uses NOW() (transaction start time) rather than
--   clock_timestamp() (wall clock) because all updates in a single
--   transaction should share the same timestamp for consistency.
-- =====================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'update_updated_at') THEN
        CREATE FUNCTION update_updated_at()
        RETURNS TRIGGER AS $func$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql;
        RAISE NOTICE 'Created update_updated_at() function';
    ELSE
        RAISE NOTICE 'update_updated_at() function already exists, skipping';
    END IF;
END $$;

COMMENT ON FUNCTION update_updated_at() IS
    'Auto-update trigger function for updated_at columns. '
    'Fires BEFORE UPDATE FOR EACH ROW. '
    'Uses NOW() (transaction start time) rather than clock_timestamp() '
    'so all updates in one transaction share the same timestamp. '
    'Shared by all tables that have an updated_at column (ARCH-01).';


-- #####################################################################
-- TABLE 1. DRUGS — Master drug table, unified across all 7 data sources
-- #####################################################################
-- WHY unified: Drugs appear in multiple databases (ChEMBL, DrugBank,
--   PubChem) with different IDs. A single canonical record per drug
--   is essential for cross-database analysis and knowledge graph
--   construction. Without unification, the same drug would appear as
--   multiple entities, corrupting interaction counts and prediction
--   scores.
-- WHY InChIKey as unique key: InChIKey is the only universally unique,
--   database-independent chemical identifier standardized by IUPAC.
--   It is computed from molecular structure, not assigned by any
--   database. This makes it ideal for entity resolution across
--   ChEMBL, DrugBank, and PubChem.
-- Data sources: ChEMBL (primary), DrugBank, PubChem
-- Key constraints: inchikey UNIQUE NOT NULL, max_phase 0-4,
--   molecular_weight positive, is_fda_approved boolean
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating drugs table...';
END $$;

CREATE TABLE IF NOT EXISTS drugs (
    -- [CMP-01, IDEM-04] GENERATED ALWAYS AS IDENTITY replaces deprecated SERIAL
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- [SCI-01] InChIKey widened from VARCHAR(27) to VARCHAR(50).
    --   Standard IUPAC InChIKeys are exactly 27 chars:
    --   14-char connectivity layer + hyphen + 10-char hash layer +
    --   hyphen + 1-char protonation indicator.
    --   Synthetic/generated InChIKeys (prefixed 'SYNTH') may exceed 27.
    --   A truncated InChIKey would break cross-database entity resolution
    --   in entity_resolution/drug_resolver.py.
    inchikey            VARCHAR(50) NOT NULL,
    name                VARCHAR(500) NOT NULL,
    -- [DQ-01] chembl_id follows pattern CHEMBL + number (e.g., CHEMBL25)
    chembl_id           VARCHAR(20),
    -- [DQ-01] drugbank_id follows pattern DB + number (e.g., DB00945)
    drugbank_id         VARCHAR(10),
    pubchem_cid         BIGINT,
    molecular_formula   VARCHAR(200),
    -- [SCI-04, SCI-07] NUMERIC(12,6) instead of FLOAT.
    --   FLOAT (IEEE 754) introduces representation errors like
    --   180.06338800000002 for glucose. Molecular fingerprinting and
    --   Tanimoto similarity calculations require exact decimal precision.
    --   Range: 0.000001 to 999999.999999 g/mol with 6 decimal places.
    molecular_weight    NUMERIC(12,6),
    -- [SCI-08] SMILES (Simplified Molecular Input Line Entry System).
    --   Full chemical validity (ring closure, valence) is validated by
    --   RDKit at the application layer. The DB provides character-set
    --   validation as a first line of defense. Capped at 50000 instead
    --   of unbounded TEXT — the longest known SMILES is ~50K chars.
    smiles              VARCHAR(50000),
    -- [DQ-02] Boolean with CHECK for SQLite cross-dialect compatibility.
    --   SQLite stores BOOLEAN as INTEGER; without CHECK, values like
    --   2 or -1 are accepted. server_default='0' ensures backward compat.
    is_fda_approved     BOOLEAN NOT NULL DEFAULT FALSE,
    -- [SCI-02] Highest clinical trial phase reached.
    --   0=Pre-clinical, 1=Phase I, 2=Phase II, 3=Phase III, 4=Approved.
    --   The RL hypothesis ranker uses max_phase >= 4 to identify
    --   approved drugs for market opportunity scoring.
    max_phase           INTEGER,
    drug_type           VARCHAR(50),
    mechanism_of_action VARCHAR(5000),
    -- [DES-08, REL-01] Soft-delete columns.
    --   Accidentally deleting an FDA-approved drug and all its interactions
    --   requires a full pipeline re-run from all 7 databases (potentially
    --   hours). Soft delete provides an undo mechanism.
    --   Matches SoftDeleteMixin in database/base.py.
    is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at          TIMESTAMP WITH TIME ZONE,
    -- [DES-06, ARCH-03, ARCH-05] Timestamps from TimestampMixin.
    --   created_at uses server_default=NOW() for insert time.
    --   updated_at is auto-updated by the update_updated_at() trigger
    --   because SQLAlchemy onupdate does NOT fire for bulk operations.
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ===================== CONSTRAINTS (drugs) =====================

    -- [SCI-01] InChIKey format: standard 27-char IUPAC, OR SYNTH-prefixed
    --   (clearly-labelled synthetic identifier for dev fixtures only).
    --   v28 ROOT FIX (audit TOP-17): the original migration accepted
    --   TEST/OUTER/INNER/IK% prefixes that the Python validator
    --   ``validate_inchikey`` in phase2/drugos_graph/config.py NEVER
    --   accepted (Python regex is strict 27-char). This divergence
    --   caused dev DBs (ORM-created) to accept biologic identifiers
    --   that production DBs (migration-created) rejected — exactly the
    --   "same schema everywhere" guarantee broken (audit finding 3).
    --   The TEST/OUTER/INNER/IK% clauses are removed here AND in
    --   migration 009 (which ALTERs existing DBs to match). SYNTH% is
    --   retained because every dev fixture in tests/fixtures/ uses
    --   SYNTH0001..SYNTH9999 as clearly-labelled synthetic identifiers
    --   — they are not chemistry and are visually unambiguous.
    --   See migration 009_tighten_inchikey_check_constraint.sql for the
    --   ALTER-side counterpart that brings existing DBs to parity.
    --   v29 ROOT FIX (audit D-2): the canonical regex is enforced
    --   AUTHORITATIVELY at the Python layer
    --   (cleaning._constants.is_canonical_inchikey). The DB CHECK uses
    --   the portable LENGTH=27 OR LIKE 'SYNTH%' form because the
    --   PostgreSQL regex operator ~ is NOT supported by SQLite.
    CONSTRAINT chk_drugs_inchikey_format
        CHECK (
            LENGTH(inchikey) = 27
            OR inchikey LIKE 'SYNTH%'
        ),
    -- [SCI-02] Clinical phase range 0-4
    CONSTRAINT chk_drugs_max_phase
        CHECK (max_phase IS NULL OR max_phase BETWEEN 0 AND 4),
    -- [DQ-02] Boolean CHECK for SQLite compatibility
    CONSTRAINT chk_drugs_is_fda_approved
        CHECK (is_fda_approved IN (0, 1)),
    -- [DQ-04] Drug name minimum 2 characters (a single letter is not a name)
    CONSTRAINT chk_drugs_name_min_length
        CHECK (LENGTH(name) >= 2),
    -- [SCI-04] Molecular weight must be positive (negative mass is unphysical)
    CONSTRAINT chk_drugs_molecular_weight_positive
        CHECK (molecular_weight IS NULL OR molecular_weight > 0),
    -- [SCI-08] SMILES character set validation (first line of defense)
    CONSTRAINT chk_drugs_smiles_valid
        CHECK (smiles IS NULL OR smiles ~ '^[A-Za-z0-9@+\-\[\]\(\)\/\#=.,!~_:;]+$')
);

-- Unique constraint on inchikey (natural key)
CREATE UNIQUE INDEX IF NOT EXISTS uq_drugs_inchikey ON drugs (inchikey);

-- [DQ-01] Partial unique indexes: allow multiple NULLs but prevent
--   duplicate non-NULL values. chembl_id and drugbank_id are nullable
--   (some drugs exist in only one source), but when present they must
--   be unique to prevent entity resolution from creating duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS uq_drugs_chembl_id
    ON drugs (chembl_id) WHERE chembl_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_drugs_drugbank_id
    ON drugs (drugbank_id) WHERE drugbank_id IS NOT NULL;

-- Indexes (CMP-02: follow ORM naming convention ix_%(table)s_%(column)s)
-- [PERF-02] Removed redundant index on inchikey (UNIQUE index already covers it)
CREATE INDEX IF NOT EXISTS ix_drugs_chembl_id ON drugs (chembl_id);
CREATE INDEX IF NOT EXISTS ix_drugs_drugbank_id ON drugs (drugbank_id);
-- [PERF-03] Index on name for search queries
CREATE INDEX IF NOT EXISTS ix_drugs_name ON drugs (name);

-- Trigger for auto-updating updated_at (ARCH-01, IDEM-01)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_drugs_updated_at'
    ) THEN
        CREATE TRIGGER trg_drugs_updated_at
            BEFORE UPDATE ON drugs
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
        RAISE NOTICE 'Created trigger trg_drugs_updated_at on drugs';
    ELSE
        RAISE NOTICE 'Trigger trg_drugs_updated_at already exists on drugs, skipping';
    END IF;
END $$;

-- COMMENT ON TABLE
COMMENT ON TABLE drugs IS
    'Master drug table — unified across all 7 data sources (ChEMBL, DrugBank, PubChem). '
    'Each row represents a unique chemical compound identified by its IUPAC InChIKey. '
    'InChIKey is the universal, database-independent identifier that enables cross-source '
    'entity resolution. Soft-delete enabled (is_deleted, deleted_at) to prevent accidental '
    'data loss that would require a full pipeline re-run.';

COMMENT ON COLUMN drugs.id IS
    'Auto-incrementing identity primary key (CMP-01: GENERATED ALWAYS AS IDENTITY).';
COMMENT ON COLUMN drugs.inchikey IS
    'IUPAC International Chemical Identifier Key. Standard format: 14-char connectivity '
    'layer + hyphen + 10-char hash layer + hyphen + 1-char protonation = 27 chars. '
    'Synthetic keys (prefixed SYNTH) may exceed 27 chars. Widened to VARCHAR(50) (SCI-01). '
    'This is the primary matching key for entity resolution across ChEMBL, DrugBank, PubChem.';
COMMENT ON COLUMN drugs.name IS
    'Canonical drug name (e.g., "Aspirin", "Acetylsalicylic acid"). Minimum 2 characters. '
    'Unified across sources by entity resolution pipeline.';
COMMENT ON COLUMN drugs.chembl_id IS
    'ChEMBL database identifier (e.g., "CHEMBL25" for Aspirin). Nullable — not all drugs '
    'are in ChEMBL. Partial unique index prevents duplicate non-NULL values (DQ-01).';
COMMENT ON COLUMN drugs.drugbank_id IS
    'DrugBank database identifier (e.g., "DB00945" for Aspirin). Nullable — not all drugs '
    'are in DrugBank. Partial unique index prevents duplicate non-NULL values (DQ-01).';
COMMENT ON COLUMN drugs.pubchem_cid IS
    'PubChem Compound ID (BIGINT for IDs exceeding 2^31). Nullable — not all drugs are in PubChem.';
COMMENT ON COLUMN drugs.molecular_formula IS
    'Molecular formula string (e.g., "C9H8O4" for Aspirin). Max 200 chars.';
COMMENT ON COLUMN drugs.molecular_weight IS
    'Molecular weight in g/mol with 6 decimal places (SCI-07: NUMERIC(12,6) instead of FLOAT). '
    'Must be positive. Example: 180.063388 for glucose. Float would store 180.06338800000002, '
    'corrupting Tanimoto similarity calculations in the entity resolver.';
COMMENT ON COLUMN drugs.smiles IS
    'SMILES (Simplified Molecular Input Line Entry System) string. Capped at 50000 chars. '
    'Full chemical validity (ring closure, valence) is validated by RDKit at the application '
    'layer. The database provides basic character-set validation as a first line of defense (SCI-08).';
COMMENT ON COLUMN drugs.is_fda_approved IS
    'Whether this drug has FDA approval. Boolean with CHECK for SQLite cross-dialect '
    'compatibility (DQ-02). 0=not approved, 1=approved. Used by RL hypothesis ranker '
    'as a safety signal filter.';
COMMENT ON COLUMN drugs.max_phase IS
    'Highest clinical trial phase reached. 0=Pre-clinical, 1=Phase I, 2=Phase II, '
    '3=Phase III, 4=FDA-approved/marketed (SCI-02). The RL agent uses max_phase >= 4 '
    'to identify approved drugs for market opportunity scoring.';
COMMENT ON COLUMN drugs.drug_type IS
    'Drug classification (e.g., "small_molecule", "antibody", "protein"). '
    'Constrained at ORM level by DrugType enum.';
COMMENT ON COLUMN drugs.mechanism_of_action IS
    'Mechanism of action description (IUPHAR/BPS Guide to Pharmacology format). '
    'Capped at 5000 chars (DQ-07).';
COMMENT ON COLUMN drugs.is_deleted IS
    'Soft-delete flag (DES-08). TRUE means this record is logically deleted but '
    'physically retained. Filter with WHERE is_deleted = FALSE in queries.';
COMMENT ON COLUMN drugs.deleted_at IS
    'Timestamp when this record was soft-deleted. NULL if not deleted.';
COMMENT ON COLUMN drugs.created_at IS
    'Record creation timestamp (server time, timezone-aware). Set once on INSERT.';
COMMENT ON COLUMN drugs.updated_at IS
    'Record last-update timestamp. Auto-updated by update_updated_at() trigger on '
    'every UPDATE. Uses NOW() (transaction start) for consistency within a transaction.';


-- #####################################################################
-- TABLE 2. PROTEINS — Protein/target table sourced from UniProt
-- #####################################################################
-- WHY UniProt ID as unique key: UniProt accessions are the global
--   standard for protein identification. They are stable, unique,
--   and used by all 7 data sources for protein cross-referencing.
-- WHY gene_symbol separate from gene_name: gene_name is a legacy
--   column that stores canonical protein NAMES (e.g., "Hemoglobin
--   subunit alpha"), NOT gene symbols (e.g., "HBA1"). The
--   gene_symbol column stores actual HGNC gene symbols used for
--   gene-disease association resolution.
-- Data sources: UniProt (primary), STRING (string_id), ChEMBL
-- Key constraints: uniprot_id UNIQUE NOT NULL, format validation,
--   sequence amino-acid validation, organism controlled vocabulary
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating proteins table...';
END $$;

CREATE TABLE IF NOT EXISTS proteins (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- [SCI-05] UniProt accession: reduced from VARCHAR(20) to VARCHAR(10).
    --   UniProt accessions are exactly 6 chars (old format: P12345) or
    --   10 chars (new format: A0A0K3AVT9). VARCHAR(20) allowed invalid IDs.
    uniprot_id      VARCHAR(10) NOT NULL,
    -- DEPRECATED: gene_name stores CANONICAL PROTEIN NAME, NOT gene symbol.
    -- DO NOT REMOVE — backward compatibility. Use gene_symbol for actual
    -- gene symbols and protein_name for full protein names.
    -- [COD-02] Kept at VARCHAR(500) for backward compatibility with existing data
    gene_name       VARCHAR(500),
    -- Actual HGNC gene symbol (e.g., "HBA1") used for GDA resolution
    gene_symbol     VARCHAR(50),
    protein_name    TEXT,
    -- [DQ-04] Controlled organism vocabulary. This platform focuses on
    --   human proteins. The CHECK constraint allows common variants
    --   ("Homo sapiens", "human", "H. sapiens") while preventing
    --   completely invalid organisms.
    organism        VARCHAR(100),
    -- [SCI-08] Amino acid sequence. Full validation (20 standard +
    --   ambiguity codes) at ORM level. Capped at 50000 — longest
    --   known protein (titin) is ~35,000 amino acids.
    sequence        VARCHAR(50000),
    function_desc   VARCHAR(10000),
    string_id       VARCHAR(50),
    -- [DES-08, REL-01] Soft-delete columns (matches SoftDeleteMixin)
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at      TIMESTAMP WITH TIME ZONE,
    -- [DES-06, ARCH-03] Timestamps from TimestampMixin
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ===================== CONSTRAINTS (proteins) =====================

    -- [SCI-05] UniProt accession format: 6 or 10 alphanumeric chars
    -- SCI-FIX: relaxed lower bound from 6 to 4 to match the ORM
    -- constraint in database/models.py and the Python validator in
    -- database/loaders.py (which both accept short test accessions
    -- like ``P001`` for dev fixtures). Real UniProt accessions are
    -- always 6 or 10 chars, but allowing >= 4 keeps dev and prod
    -- schemas in sync so test fixtures don't silently fail on
    -- migration-created databases (audit finding 4).
    CONSTRAINT chk_proteins_uniprot_length
        CHECK (LENGTH(uniprot_id) >= 4 AND LENGTH(uniprot_id) <= 10),
    -- [DQ-04] Organism controlled vocabulary
    --   Allows common variants of human while preventing invalid organisms.
    --   SCI-FIX: the original CHECK rejected ALL non-human organisms,
    --   which silently broke cross-species protein ingestion. The
    --   codebase's entity_resolution/protein_resolver.py ships an
    --   _ORGANISM_ALIASES map covering mouse, rat, e.coli, yeast,
    --   fly, worm, and zebrafish — and _UNIPROT_ORGANISM_OVERRIDES
    --   whitelists specific mouse (P02340 = Trp53) and rat (P04631)
    --   accessions. The previous CHECK made it impossible to land
    --   ANY of those proteins in the staging DB, silently dropping
    --   mouse homolog evidence from the knowledge graph (audit
    --   finding 1). The fix below allows the model organisms that
    --   the rest of the codebase already supports, plus the
    --   previously-allowed human variants and a catch-all "unknown
    --   organism" string used by handle_missing_protein_fields.
    CONSTRAINT chk_proteins_organism
        CHECK (
            organism IS NULL
            OR LOWER(TRIM(organism)) IN (
                'homo sapiens', 'human', 'humans', 'h. sapiens',
                'mus musculus', 'mouse', 'mice', 'm. musculus',
                'rattus norvegicus', 'rat', 'rats', 'r. norvegicus',
                'escherichia coli', 'e. coli', 'e.coli',
                'saccharomyces cerevisiae', 'yeast', 's. cerevisiae',
                'drosophila melanogaster', 'fruit fly', 'd. melanogaster',
                'caenorhabditis elegans', 'c. elegans', 'nematode',
                'danio rerio', 'zebrafish', 'd. rerio',
                'unknown organism', 'unknown', ''
            )
        ),
    -- [DQ-02] Boolean CHECK for is_deleted (SQLite compatibility)
    CONSTRAINT chk_proteins_is_deleted
        CHECK (is_deleted IN (0, 1))
);

-- Unique constraint on uniprot_id (natural key)
CREATE UNIQUE INDEX IF NOT EXISTS uq_proteins_uniprot_id ON proteins (uniprot_id);

-- Indexes (CMP-02: ORM naming convention)
-- [PERF-02] Removed redundant index on uniprot_id (UNIQUE already indexes it)
CREATE INDEX IF NOT EXISTS ix_proteins_gene_symbol ON proteins (gene_symbol);
CREATE INDEX IF NOT EXISTS ix_proteins_gene_name ON proteins (gene_name);
CREATE INDEX IF NOT EXISTS ix_proteins_string_id ON proteins (string_id);

-- Trigger for auto-updating updated_at
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_proteins_updated_at'
    ) THEN
        CREATE TRIGGER trg_proteins_updated_at
            BEFORE UPDATE ON proteins
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
        RAISE NOTICE 'Created trigger trg_proteins_updated_at on proteins';
    ELSE
        RAISE NOTICE 'Trigger trg_proteins_updated_at already exists on proteins, skipping';
    END IF;
END $$;

COMMENT ON TABLE proteins IS
    'Protein/target table sourced primarily from UniProt. Each row represents a unique '
    'protein identified by its UniProt accession. Key data includes gene symbol, protein '
    'name, amino acid sequence, and functional description. The gene_name column is '
    'DEPRECATED — it stores canonical protein names, NOT gene symbols. Use gene_symbol '
    'for actual HGNC gene symbols. Soft-delete enabled.';

COMMENT ON COLUMN proteins.id IS
    'Auto-incrementing identity primary key (CMP-01).';
COMMENT ON COLUMN proteins.uniprot_id IS
    'UniProt accession identifier. Format: 6 chars (old, e.g., P69999) or 10 chars '
    '(new, e.g., A0A0K3AVT9). This is the primary matching key for protein entity '
    'resolution across STRING, DisGeNET, OMIM, and ChEMBL (SCI-05).';
COMMENT ON COLUMN proteins.gene_name IS
    'DEPRECATED — stores canonical PROTEIN name (e.g., "Hemoglobin subunit alpha"), '
    'NOT gene symbol. Retained for backward compatibility. Use gene_symbol for gene '
    'symbols and protein_name for protein names.';
COMMENT ON COLUMN proteins.gene_symbol IS
    'HGNC gene symbol (e.g., "HBA1", "TP53"). Used for gene-disease association '
    'resolution in DisGeNET/OMIM pipelines. Validated at ORM level against HGNC format.';
COMMENT ON COLUMN proteins.protein_name IS
    'Full protein name (e.g., "Hemoglobin subunit alpha"). From UniProt description field.';
COMMENT ON COLUMN proteins.organism IS
    'Source organism. Constrained to common human variants (Homo sapiens, human, H. sapiens). '
    'This platform focuses on human proteins — other organisms are filtered out (DQ-04).';
COMMENT ON COLUMN proteins.sequence IS
    'Amino acid sequence using 20 standard + ambiguity codes (B, J, O, U, X, Z, *). '
    'Validated at ORM level. Capped at 50000 chars — longest known protein (titin) '
    'is ~35,000 amino acids (SCI-08).';
COMMENT ON COLUMN proteins.function_desc IS
    'UniProt functional annotation text. Capped at 10000 chars (DQ-07).';
COMMENT ON COLUMN proteins.string_id IS
    'STRING database protein identifier (e.g., "9606.ENSP00000269305"). Used for '
    'protein-protein interaction network mapping.';
COMMENT ON COLUMN proteins.is_deleted IS
    'Soft-delete flag (DES-08). TRUE = logically deleted but physically retained.';
COMMENT ON COLUMN proteins.deleted_at IS
    'Timestamp when soft-deleted. NULL if not deleted.';
COMMENT ON COLUMN proteins.created_at IS
    'Record creation timestamp (server time, timezone-aware).';
COMMENT ON COLUMN proteins.updated_at IS
    'Record last-update timestamp. Auto-updated by trigger.';


-- #####################################################################
-- TABLE 3. DRUG–PROTEIN INTERACTIONS — from ChEMBL + DrugBank
-- #####################################################################
-- WHY this table: The drug-protein interaction is the central edge in
--   the knowledge graph. Drug → targets → Protein → pathway → Disease.
--   Without accurate interactions, the Graph Transformer has no edges
--   to learn from, and all predictions are meaningless.
-- WHY activity_type/value cross-validation: Each activity type (IC50,
--   Ki, Kd, EC50) has different valid ranges and units. IC50 of -5
--   or 999999999 would be silently accepted without CHECK, corrupting
--   the interaction scoring used by the Graph Transformer.
-- Data sources: ChEMBL (primary), DrugBank
-- Key constraints: activity_value > 0, confidence 0-1, source enum
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating drug_protein_interactions table...';
END $$;

CREATE TABLE IF NOT EXISTS drug_protein_interactions (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    drug_id         INTEGER NOT NULL REFERENCES drugs(id) ON DELETE CASCADE,
    protein_id      INTEGER NOT NULL REFERENCES proteins(id) ON DELETE CASCADE,
    interaction_type VARCHAR(50),
    -- [SCI-06] Activity value must be positive. Negative IC50/Ki/Kd/EC50
    --   is scientifically meaningless.
    activity_value  FLOAT,
    -- [SCI-06] Activity type enum — each has different valid ranges/units
    activity_type   VARCHAR(20),
    -- [SCI-06] Activity units enum — must match the type
    activity_units  VARCHAR(20),
    -- [COD-05] Source constrained to valid pipeline names
    source          VARCHAR(20),
    -- [DES-04] source_id is nullable. Empty string conflated with "no value"
    --   — NULL is semantically correct for missing source IDs.
    source_id       VARCHAR(50),
    -- [DQ-03] Confidence score on [0.0, 1.0] probability scale
    confidence_score FLOAT,
    -- [LINE-01] Source version tracking for provenance
    source_version  VARCHAR(50),
    -- [LINE-01] When this record's source data was fetched
    source_fetch_date TIMESTAMP WITH TIME ZONE,
    -- [LINE-01] Whether entity resolution has been applied to this record
    entity_resolved BOOLEAN DEFAULT FALSE,
    -- [IDEM-02, LOG-03] Pipeline run that produced this record
    pipeline_run_id INTEGER REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    -- [DES-06] Timestamps
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ===================== CONSTRAINTS (DPI) =====================

    -- [SCI-06] Activity value must be positive
    CONSTRAINT chk_dpi_activity_value_positive
        CHECK (activity_value IS NULL OR activity_value > 0),
    -- [SCI-06] Activity type enum
    CONSTRAINT chk_dpi_activity_type
        CHECK (activity_type IS NULL OR activity_type IN
            ('IC50', 'Ki', 'Kd', 'EC50', 'AC50', 'Potency',
             'Selectivity', 'Efficacy', 'Agonist', 'Antagonist',
             'Inhibitor', 'Modulator')),
    -- [SCI-06] Activity units enum
    CONSTRAINT chk_dpi_activity_units
        CHECK (activity_units IS NULL OR activity_units IN
            ('nM', 'uM', 'mM', 'M', '%', 'mg/mL', 'ug/mL')),
    -- [DQ-03] Confidence score on [0.0, 1.0] probability scale
    CONSTRAINT chk_dpi_confidence_score_range
        CHECK (confidence_score IS NULL OR
               (confidence_score >= 0.0 AND confidence_score <= 1.0)),
    -- [COD-05] Source must be a valid pipeline name
    CONSTRAINT chk_dpi_source
        CHECK (source IS NULL OR source IN ('chembl', 'drugbank')),

    -- Unique constraint: one interaction per (drug, protein, source, source_id)
    CONSTRAINT uq_dpi_drug_protein_source
        UNIQUE (drug_id, protein_id, source, source_id)
);

-- Indexes (CMP-02: ORM naming convention)
CREATE INDEX IF NOT EXISTS ix_drug_protein_interactions_drug_id ON drug_protein_interactions (drug_id);
CREATE INDEX IF NOT EXISTS ix_drug_protein_interactions_protein_id ON drug_protein_interactions (protein_id);
-- [PERF-01] Composite indexes for common query patterns
CREATE INDEX IF NOT EXISTS ix_dpi_protein_interaction
    ON drug_protein_interactions (protein_id, interaction_type);
CREATE INDEX IF NOT EXISTS ix_dpi_drug_interaction
    ON drug_protein_interactions (drug_id, interaction_type);

-- Trigger for auto-updating updated_at
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_dpi_updated_at'
    ) THEN
        CREATE TRIGGER trg_dpi_updated_at
            BEFORE UPDATE ON drug_protein_interactions
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
        RAISE NOTICE 'Created trigger trg_dpi_updated_at on drug_protein_interactions';
    ELSE
        RAISE NOTICE 'Trigger trg_dpi_updated_at already exists, skipping';
    END IF;
END $$;

COMMENT ON TABLE drug_protein_interactions IS
    'Drug-protein interaction records from ChEMBL and DrugBank. Each row represents '
    'a measured interaction between a drug and a protein target. This is the central '
    'edge in the knowledge graph: Drug -> targets -> Protein -> pathway -> Disease. '
    'Includes lineage tracking (source_version, source_fetch_date, pipeline_run_id) '
    'for provenance and reproducibility.';

COMMENT ON COLUMN drug_protein_interactions.id IS
    'Auto-incrementing identity primary key (CMP-01).';
COMMENT ON COLUMN drug_protein_interactions.drug_id IS
    'FK to drugs.id. ON DELETE CASCADE — when a drug is deleted, its interactions go too.';
COMMENT ON COLUMN drug_protein_interactions.protein_id IS
    'FK to proteins.id. ON DELETE CASCADE — when a protein is deleted, its interactions go too.';
COMMENT ON COLUMN drug_protein_interactions.interaction_type IS
    'Type of drug-protein interaction (e.g., "inhibitor", "activator", "agonist", '
    '"antagonist", "modulator"). Constrained at ORM level by InteractionType enum.';
COMMENT ON COLUMN drug_protein_interactions.activity_value IS
    'Measured activity value (e.g., IC50 = 100 nM). Must be positive (SCI-06). '
    'Interpretation depends on activity_type: IC50/Ki/Kd in nM, EC50 in uM, etc.';
COMMENT ON COLUMN drug_protein_interactions.activity_type IS
    'Type of activity measurement. Valid values: IC50, Ki, Kd, EC50, AC50, Potency, '
    'Selectivity, Efficacy, Agonist, Antagonist, Inhibitor, Modulator (SCI-06).';
COMMENT ON COLUMN drug_protein_interactions.activity_units IS
    'Units of the activity value. Valid: nM, uM, mM, M, %, mg/mL, ug/mL (SCI-06). '
    'Cross-validation with activity_type should be done at the application layer.';
COMMENT ON COLUMN drug_protein_interactions.source IS
    'Source database for this interaction. Must be "chembl" or "drugbank" (COD-05).';
COMMENT ON COLUMN drug_protein_interactions.source_id IS
    'Source-specific identifier for this record. Nullable (DES-04) — NULL means '
    'no source ID was available. Used in unique constraint with NULL handling.';
COMMENT ON COLUMN drug_protein_interactions.confidence_score IS
    'Interaction confidence score on [0.0, 1.0] probability scale (DQ-03). '
    'Used by the Graph Transformer for edge weighting during message passing.';
COMMENT ON COLUMN drug_protein_interactions.source_version IS
    'Version of the source database (e.g., "ChEMBL 34"). For lineage tracking '
    '— distinguishes records from different source versions (LINE-01).';
COMMENT ON COLUMN drug_protein_interactions.source_fetch_date IS
    'When this record''s source data was fetched from the API. For lineage (LINE-01).';
COMMENT ON COLUMN drug_protein_interactions.entity_resolved IS
    'Whether entity resolution has been applied to this record. FALSE until the '
    'entity resolution pipeline processes it (LINE-01).';
COMMENT ON COLUMN drug_protein_interactions.pipeline_run_id IS
    'FK to pipeline_runs.id. Tracks which pipeline run produced this record '
    'for reproducibility (IDEM-02, LOG-03). ON DELETE SET NULL if run is deleted.';
COMMENT ON COLUMN drug_protein_interactions.created_at IS
    'Record creation timestamp (server time, timezone-aware).';
COMMENT ON COLUMN drug_protein_interactions.updated_at IS
    'Record last-update timestamp. Auto-updated by trigger.';


-- #####################################################################
-- TABLE 4. PROTEIN–PROTEIN INTERACTIONS — from STRING
-- #####################################################################
-- WHY STRING scores are 0-1000 (NOT 0-100): This is the #1 scientific
--   correctness issue. STRING documentation explicitly states scores
--   range from 0 to 1000. A common misconception treats them as
--   percentages. The Graph Transformer uses PPI scores as edge weights
--   during message passing — corrupted scores produce incorrect node
--   embeddings that cascade into wrong drug-disease predictions.
-- WHY protein_a_id < protein_b_id: Prevents symmetric duplicates
--   (A→B and B→A). The ordered constraint ensures each interaction
--   is stored exactly once, which is critical for idempotent pipeline
--   re-runs.
-- Data sources: STRING (primary)
-- Key constraints: scores 0-1000, ordered pair, source required
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating protein_protein_interactions table...';
END $$;

CREATE TABLE IF NOT EXISTS protein_protein_interactions (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    protein_a_id    INTEGER NOT NULL REFERENCES proteins(id) ON DELETE CASCADE,
    protein_b_id    INTEGER NOT NULL REFERENCES proteins(id) ON DELETE CASCADE,
    -- [SCI-03] STRING scores are 0-1000. NOT 0-100.
    --   combined_score: weighted combination of all evidence channels
    combined_score  INTEGER,
    --   experimental_score: from experimental evidence (binding assays, etc.)
    experimental_score INTEGER,
    --   database_score: from curated pathway databases
    database_score  INTEGER,
    --   textmining_score: from text mining of PubMed abstracts
    textmining_score INTEGER,
    -- [COD-05, CFG-03] Source is required — no default. Must be explicitly
    --   specified to prevent ambiguity about data provenance.
    source          VARCHAR(20) NOT NULL,
    -- [INT-04] Score JSON for source-specific payloads beyond STRING
    score_json      TEXT,
    -- [IDEM-02, LOG-03] Pipeline run tracking
    pipeline_run_id INTEGER REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    -- [DES-06] Timestamps
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ===================== CONSTRAINTS (PPI) =====================

    -- [DES-02, IDEM-03] Prevent symmetric duplicates: protein_a_id must be < protein_b_id.
    --   This ensures (A,B) and (B,A) cannot both exist. The entity resolution
    --   pipeline must swap IDs to ensure ordering before INSERT.
    CONSTRAINT chk_ppi_ordered
        CHECK (protein_a_id < protein_b_id),
    -- [SCI-03] Score bounds — all STRING scores are 0-1000
    CONSTRAINT chk_ppi_combined_score
        CHECK (combined_score IS NULL OR (combined_score >= 0 AND combined_score <= 1000)),
    CONSTRAINT chk_ppi_experimental_score
        CHECK (experimental_score IS NULL OR (experimental_score >= 0 AND experimental_score <= 1000)),
    CONSTRAINT chk_ppi_database_score
        CHECK (database_score IS NULL OR (database_score >= 0 AND database_score <= 1000)),
    CONSTRAINT chk_ppi_textmining_score
        CHECK (textmining_score IS NULL OR (textmining_score >= 0 AND textmining_score <= 1000)),
    -- [COD-05] Source validation
    CONSTRAINT chk_ppi_source
        CHECK (source IN ('string')),

    -- Unique constraint: one interaction per ordered protein pair
    CONSTRAINT uq_ppi_protein_pair
        UNIQUE (protein_a_id, protein_b_id)
);

-- Indexes (CMP-02: ORM naming convention)
CREATE INDEX IF NOT EXISTS ix_protein_protein_interactions_protein_a_id
    ON protein_protein_interactions (protein_a_id);
CREATE INDEX IF NOT EXISTS ix_protein_protein_interactions_protein_b_id
    ON protein_protein_interactions (protein_b_id);

-- Trigger for auto-updating updated_at
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_ppi_updated_at'
    ) THEN
        CREATE TRIGGER trg_ppi_updated_at
            BEFORE UPDATE ON protein_protein_interactions
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
        RAISE NOTICE 'Created trigger trg_ppi_updated_at on protein_protein_interactions';
    ELSE
        RAISE NOTICE 'Trigger trg_ppi_updated_at already exists, skipping';
    END IF;
END $$;

COMMENT ON TABLE protein_protein_interactions IS
    'Protein-protein interaction records from STRING database. Each row represents an '
    'interaction between two proteins. CRITICAL: STRING scores range 0-1000, NOT 0-100. '
    'Score misinterpretation corrupts Graph Transformer edge weights (SCI-03). '
    'Protein pairs are ordered (protein_a_id < protein_b_id) to prevent symmetric duplicates.';

COMMENT ON COLUMN protein_protein_interactions.id IS
    'Auto-incrementing identity primary key (CMP-01).';
COMMENT ON COLUMN protein_protein_interactions.protein_a_id IS
    'FK to proteins.id. Must be less than protein_b_id (DES-02) to prevent '
    'symmetric duplicates like (A,B) and (B,A). ON DELETE CASCADE.';
COMMENT ON COLUMN protein_protein_interactions.protein_b_id IS
    'FK to proteins.id. Must be greater than protein_a_id. ON DELETE CASCADE.';
COMMENT ON COLUMN protein_protein_interactions.combined_score IS
    'STRING combined score [0-1000]. Weighted combination of all evidence channels. '
    'NOT a percentage. Divide by 1000.0 for [0,1] range expected by ML models (SCI-03).';
COMMENT ON COLUMN protein_protein_interactions.experimental_score IS
    'STRING experimental evidence score [0-1000]. From binding assays, etc.';
COMMENT ON COLUMN protein_protein_interactions.database_score IS
    'STRING curated database evidence score [0-1000]. From pathway databases.';
COMMENT ON COLUMN protein_protein_interactions.textmining_score IS
    'STRING text mining evidence score [0-1000]. From PubMed abstract analysis.';
COMMENT ON COLUMN protein_protein_interactions.source IS
    'Source database. Currently only "string" is valid (COD-05). Required (NOT NULL).';
COMMENT ON COLUMN protein_protein_interactions.score_json IS
    'JSON payload for source-specific scores beyond STRING standard fields (INT-04).';
COMMENT ON COLUMN protein_protein_interactions.pipeline_run_id IS
    'FK to pipeline_runs.id for lineage tracking (IDEM-02). ON DELETE SET NULL.';
COMMENT ON COLUMN protein_protein_interactions.created_at IS
    'Record creation timestamp.';
COMMENT ON COLUMN protein_protein_interactions.updated_at IS
    'Record last-update timestamp. Auto-updated by trigger.';


-- #####################################################################
-- TABLE 5. GENE–DISEASE ASSOCIATIONS — from DisGeNET + OMIM
-- #####################################################################
-- WHY this table: Gene-disease associations connect proteins to diseases,
--   completing the Drug → Protein → Pathway → Disease chain in the
--   knowledge graph. Without accurate GDA, the Graph Transformer cannot
--   learn the biological pathways that connect drugs to diseases.
-- WHY disease_id_type: Disease IDs come from different systems (OMIM,
--   UMLS CUI, DOID, MeSH). Without type tracking, the same disease
--   appears under different identifiers, and the RL agent cannot
--   aggregate evidence across sources.
-- Data sources: DisGeNET (primary), OMIM
-- Key constraints: gene_symbol validated, disease_id_type enum,
--   score range, unique per (gene, disease, source)
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating gene_disease_associations table...';
END $$;

CREATE TABLE IF NOT EXISTS gene_disease_associations (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- [DQ-05] gene_symbol must be non-empty — it is part of the natural key.
    --   A gene-disease association without a gene symbol is meaningless.
    gene_symbol     VARCHAR(50) NOT NULL DEFAULT '',
    -- Denormalized convenience: UniProt ID for direct protein lookup.
    -- v14 ROOT FIX (FIX4 / CD-3): the GDA table uses the STRING
    -- ``uniprot_id`` FK as the canonical cross-source protein reference.
    -- The previous code also declared an integer ``protein_id`` FK
    -- ("for fast joins") — but the loader never populated it, the
    -- backfill in migration 003 was a no-op, and the column produced
    -- an unused index + false-positive schema drift. Removed.
    uniprot_id      VARCHAR(10) REFERENCES proteins(uniprot_id) ON DELETE SET NULL,
    -- [DQ-06, SCI-07] Disease ID must be non-empty and have a known type
    disease_id      VARCHAR(50) NOT NULL DEFAULT '',
    -- [SCI-07] Disease ID type — indicates which identifier system is used.
    --   OMIM: numeric (e.g., "613325")
    --   DisGeNET/UMLS: CUI format (e.g., "C0009400")
    --   DOID: ontology ID (e.g., "DOID:1234")
    --   MeSH: descriptor ID (e.g., "D000001")
    disease_id_type VARCHAR(20),
    -- [DQ-07] Disease name capped to prevent unbounded storage
    disease_name    VARCHAR(1000),
    association_type VARCHAR(100),
    score           FLOAT,
    -- [COD-05] Source constrained to valid pipeline names
    source          VARCHAR(20),
    -- [SEC-02, DQ-07] PMID list capped at 2000 chars. Semicolon-separated
    --   PubMed IDs (e.g., "12345678;23456789;34567890").
    pmid_list       VARCHAR(2000),
    -- [LINE-03] Score computation tracking
    score_type      VARCHAR(50),
    score_method    VARCHAR(100),
    -- [IDEM-02, LOG-03] Pipeline run tracking
    pipeline_run_id INTEGER REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    -- [DES-06] Timestamps
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ===================== CONSTRAINTS (GDA) =====================

    -- [DQ-05] Gene symbol must be non-empty (part of natural key)
    CONSTRAINT chk_gda_gene_symbol
        CHECK (gene_symbol <> ''),
    -- [DQ-06] Disease ID must be non-empty
    CONSTRAINT chk_gda_disease_id
        CHECK (disease_id <> ''),
    -- [SCI-07] Disease ID type enum
    -- SCI-FIX: include 'hpo' (Human Phenotype Ontology) to match the ORM
    -- model and DisGeNET's actual data (Piñero et al. 2020). Originally
    -- 'hpo' was missing here and only added in migration 004 / ORM.
    -- CRITICAL FIX (patient safety): added 'icd10' (WHO international
    -- clinical classification), 'efo' (Experimental Factor Ontology —
    -- GWAS Catalog, UK Biobank, Open Targets), and 'orphanet' (rare-
    -- disease ontology). KEEP IN SYNC with database/models.py and
    -- database/loaders.py::_VALID_DISEASE_ID_TYPES.
    CONSTRAINT chk_gda_disease_id_type
        CHECK (disease_id_type IS NULL OR disease_id_type IN
            ('omim', 'disgenet', 'doid', 'mesh', 'umls', 'hpo',
             'icd10', 'efo', 'orphanet')),
    -- [SCI-07] Disease ID format validation based on type
    -- SCI-FIX (BUG-3.8): the OMIM pipeline emits ``disease_id = "OMIM:" + str(...)``
    -- to match DisGeNET's API format. The validator now accepts BOTH
    -- ``OMIM:\d{4,7}`` (OMIM pipeline + DisGeNET API format) AND
    -- ``\d{4,7}`` (canonical MIM number format). The HPO type is also
    -- accepted here even though it's not in the enum CHECK above (added
    -- in migration 004 / ORM model) — belt-and-suspenders.
    -- ICD-10: WHO format — letter + 2 digits + optional '.subsection'
    --   Examples: I10, E11.9, M05.1, C50.1, S72.001A
    -- EFO: OBO curie pattern "EFO:_nnnnnnn" (e.g. EFO:0000400).
    -- Orphanet: "ORPHA:nnnn" (e.g. ORPHA:585).
    CONSTRAINT chk_gda_disease_id_format
        CHECK (
            disease_id_type IS NULL
            OR (disease_id_type = 'omim'     AND disease_id ~ '^(OMIM:)?\d{4,7}$')
            OR (disease_id_type = 'disgenet' AND disease_id ~ '^C\d{7}$')
            OR (disease_id_type = 'umls'     AND disease_id ~ '^C\d{7}$')
            OR (disease_id_type = 'doid'     AND disease_id ~ '^DOID:\d+$')
            OR (disease_id_type = 'mesh'     AND disease_id ~ '^D\d{6}$')
            OR (disease_id_type = 'hpo'      AND disease_id ~ '^HP:\d{7}$')
            OR (disease_id_type = 'icd10'    AND disease_id ~ '^[A-Z]\d{2}(\.[A-Z0-9]{1,4})?$')
            OR (disease_id_type = 'efo'      AND disease_id ~ '^EFO:_\d{7,}$')
            OR (disease_id_type = 'orphanet' AND disease_id ~ '^ORPHA:\d+$')
        ),
    -- [SEC-02] PMID list format validation
    CONSTRAINT chk_gda_pmid_list
        CHECK (pmid_list IS NULL OR pmid_list ~ '^[\d;,\s]*$'),
    -- [COD-05] Source must be a valid pipeline name.
    -- v41 ROOT FIX (SEV1 #3): allow both bare 'disgenet' (legacy
    -- default) AND prefixed 'disgenet_<subsrc>' values actually
    -- emitted by disgenet_pipeline._derive_source_value (line 2620:
    -- f"disgenet_{source_id.lower()}" e.g. "disgenet_curated",
    -- "disgenet_inference", "disgenet_v7_2024_06"). The previous
    -- constraint restricted to exactly ('disgenet','omim') and
    -- rejected 100% of DisGeNET GDA rows on PostgreSQL AND SQLite.
    -- Uses LIKE with ESCAPE '|' for SQLite portability (the ~ regex
    -- operator is PostgreSQL-only and silently dropped by the
    -- migration runner's _translate_sql_for_sqlite translator).
    CONSTRAINT chk_gda_source
        CHECK (
            source IS NULL
            OR source = 'omim'
            OR source = 'disgenet'
            OR source LIKE 'disgenet|_%' ESCAPE '|'
        ),

    -- Unique constraint: one association per (gene, disease, source)
    CONSTRAINT uq_gda_gene_disease_source
        UNIQUE (gene_symbol, disease_id, source)
);

-- Indexes (CMP-02: ORM naming convention)
CREATE INDEX IF NOT EXISTS ix_gene_disease_associations_gene_symbol
    ON gene_disease_associations (gene_symbol);
CREATE INDEX IF NOT EXISTS ix_gene_disease_associations_disease_id
    ON gene_disease_associations (disease_id);
CREATE INDEX IF NOT EXISTS ix_gda_uniprot_id
    ON gene_disease_associations (uniprot_id);
-- v14 ROOT FIX (FIX4 / CD-3): ix_gda_protein_id removed — the
-- protein_id column no longer exists on gene_disease_associations.

-- Trigger for auto-updating updated_at
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_gda_updated_at'
    ) THEN
        CREATE TRIGGER trg_gda_updated_at
            BEFORE UPDATE ON gene_disease_associations
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
        RAISE NOTICE 'Created trigger trg_gda_updated_at on gene_disease_associations';
    ELSE
        RAISE NOTICE 'Trigger trg_gda_updated_at already exists, skipping';
    END IF;
END $$;

COMMENT ON TABLE gene_disease_associations IS
    'Gene-disease association records from DisGeNET and OMIM. Each row links a gene '
    '(via gene_symbol and uniprot_id) to a disease (via disease_id with type tracking). '
    'CRITICAL for knowledge graph construction: this completes the Drug->Protein->Pathway->'
    'Disease chain. Sensitive classification: gene-disease associations for rare diseases '
    'could theoretically narrow down patient identities in small populations (CMP-04).';

COMMENT ON COLUMN gene_disease_associations.id IS
    'Auto-incrementing identity primary key (CMP-01).';
COMMENT ON COLUMN gene_disease_associations.gene_symbol IS
    'HGNC gene symbol (e.g., "HBA1", "TP53"). NOT NULL and non-empty — required '
    'because it is part of the natural key for deduplication (DQ-05).';
COMMENT ON COLUMN gene_disease_associations.uniprot_id IS
    'Canonical UniProt accession FK to proteins.uniprot_id. '
    'ON DELETE SET NULL — the association remains even if the protein is deleted. '
    'v14 ROOT FIX (FIX4 / CD-3): this is the SOLE protein reference on the GDA '
    'table. The integer protein_id column was removed (loader never populated it).';
COMMENT ON COLUMN gene_disease_associations.disease_id IS
    'Disease identifier. NOT NULL — a gene-disease association without a disease ID '
    'is meaningless (DQ-06). Format varies by disease_id_type.';
COMMENT ON COLUMN gene_disease_associations.disease_id_type IS
    'Identifier system used for disease_id. Valid: omim (numeric 4-7 digits), '
    'disgenet/umls (C + 7 digits), doid (DOID: + digits), mesh (D + 6 digits). '
    'NULL means the type is unknown/unspecified (SCI-07).';
COMMENT ON COLUMN gene_disease_associations.disease_name IS
    'Human-readable disease name. Capped at 1000 chars (DQ-07).';
COMMENT ON COLUMN gene_disease_associations.association_type IS
    'Type of association (e.g., "therapeutic", "biomarker", "genetic_variation").';
COMMENT ON COLUMN gene_disease_associations.score IS
    'Association score from the source database. Interpretation varies by source '
    'and score_type column.';
COMMENT ON COLUMN gene_disease_associations.source IS
    'Source database. Must be "disgenet" or "omim" (COD-05).';
COMMENT ON COLUMN gene_disease_associations.pmid_list IS
    'Semicolon-separated PubMed IDs (e.g., "12345678;23456789"). Capped at 2000 chars '
    '(SEC-02) to prevent unbounded storage. Format validated by CHECK constraint.';
COMMENT ON COLUMN gene_disease_associations.score_type IS
    'Type of score (e.g., "gda_score", "confidence_score"). Documents how the '
    'score was computed (LINE-03).';
COMMENT ON COLUMN gene_disease_associations.score_method IS
    'Method used to compute the score (e.g., "disgenet_weighted", "pubmed_count"). '
    'For lineage and reproducibility (LINE-03).';
COMMENT ON COLUMN gene_disease_associations.pipeline_run_id IS
    'FK to pipeline_runs.id for lineage tracking (IDEM-02). ON DELETE SET NULL.';
COMMENT ON COLUMN gene_disease_associations.created_at IS
    'Record creation timestamp.';
COMMENT ON COLUMN gene_disease_associations.updated_at IS
    'Record last-update timestamp. Auto-updated by trigger.';


-- #####################################################################
-- TABLE 6. ENTITY MAPPING — Cross-database entity resolution output
-- #####################################################################
-- WHY this table: The same drug/protein appears under different IDs in
--   different databases. Entity resolution maps these to a single
--   canonical entity. Without it, Aspirin in DrugBank and
--   acetylsalicylic acid in ChEMBL would be treated as different drugs,
--   doubling interaction counts and corrupting predictions.
-- Data sources: Entity resolution pipeline (aggregates from all sources)
-- Key constraints: canonical_inchikey partial unique, match_confidence 0-1
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating entity_mapping table...';
END $$;

CREATE TABLE IF NOT EXISTS entity_mapping (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- [SCI-01] Widened from VARCHAR(27) to VARCHAR(50) for synthetic InChIKeys
    canonical_inchikey VARCHAR(50),
    canonical_name  VARCHAR(500),
    chembl_id       VARCHAR(20),
    drugbank_id     VARCHAR(10),
    pubchem_cid     BIGINT,
    uniprot_id      VARCHAR(10),
    string_id       VARCHAR(50),
    -- [DQ-03] Match confidence on [0.0, 1.0] probability scale
    match_confidence FLOAT,
    match_method    VARCHAR(50),
    -- [LINE-03, LINE-04] Full resolution attempt chain as JSON
    --   Format: [{"method":"name_match","confidence":0.7,"timestamp":"..."},
    --            {"method":"inchikey_match","confidence":0.95,"timestamp":"..."}]
    match_history   TEXT,
    -- [LINE-04] When the last resolution was performed
    last_matched_at TIMESTAMP WITH TIME ZONE,
    -- [DES-06] Timestamps
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ===================== CONSTRAINTS (entity_mapping) =====================

    -- [DQ-03] Match confidence range [0.0, 1.0]
    CONSTRAINT chk_entity_mapping_confidence_range
        CHECK (match_confidence IS NULL OR
               (match_confidence >= 0.0 AND match_confidence <= 1.0))
);

-- Partial unique indexes (DQ-01, DES-03)
-- Only enforce uniqueness for non-NULL canonical_inchikey
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_mapping_inchikey
    ON entity_mapping (canonical_inchikey)
    WHERE canonical_inchikey IS NOT NULL;
-- Unique name for records without InChIKey
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_mapping_name_no_inchikey
    ON entity_mapping (canonical_name)
    WHERE canonical_inchikey IS NULL AND canonical_name IS NOT NULL;
-- [DES-03] Unique chembl_id where not null
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_mapping_chembl
    ON entity_mapping (chembl_id) WHERE chembl_id IS NOT NULL;
-- [DES-03] Unique drugbank_id where not null
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_mapping_drugbank
    ON entity_mapping (drugbank_id) WHERE drugbank_id IS NOT NULL;

-- Trigger for auto-updating updated_at
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_entity_mapping_updated_at'
    ) THEN
        CREATE TRIGGER trg_entity_mapping_updated_at
            BEFORE UPDATE ON entity_mapping
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
        RAISE NOTICE 'Created trigger trg_entity_mapping_updated_at on entity_mapping';
    ELSE
        RAISE NOTICE 'Trigger trg_entity_mapping_updated_at already exists, skipping';
    END IF;
END $$;

COMMENT ON TABLE entity_mapping IS
    'Cross-database entity resolution output. Maps the same drug/protein across '
    'databases (ChEMBL, DrugBank, UniProt, STRING) to a single canonical entity. '
    'When a canonical InChIKey is available, it serves as the primary identifier; '
    'otherwise canonical_name is used. Includes resolution history (match_history) '
    'for algorithm improvement auditing (LINE-04).';

COMMENT ON COLUMN entity_mapping.id IS
    'Auto-incrementing identity primary key (CMP-01).';
COMMENT ON COLUMN entity_mapping.canonical_inchikey IS
    'Canonical IUPAC InChIKey for this entity. Widened to VARCHAR(50) for synthetic '
    'keys (SCI-01). Partial unique index enforces uniqueness for non-NULL values.';
COMMENT ON COLUMN entity_mapping.canonical_name IS
    'Canonical name for entities without an InChIKey. Unique among records without '
    'a canonical_inchikey (partial unique index).';
COMMENT ON COLUMN entity_mapping.chembl_id IS
    'ChEMBL identifier. Partial unique index prevents duplicate non-NULL values.';
COMMENT ON COLUMN entity_mapping.drugbank_id IS
    'DrugBank identifier. Partial unique index prevents duplicate non-NULL values.';
COMMENT ON COLUMN entity_mapping.pubchem_cid IS
    'PubChem Compound ID (BIGINT for large IDs).';
COMMENT ON COLUMN entity_mapping.uniprot_id IS
    'UniProt accession (VARCHAR(10), matches proteins.uniprot_id format).';
COMMENT ON COLUMN entity_mapping.string_id IS
    'STRING protein identifier (e.g., "9606.ENSP00000269305").';
COMMENT ON COLUMN entity_mapping.match_confidence IS
    'Resolution confidence score on [0.0, 1.0] scale (DQ-03). Higher = more certain match.';
COMMENT ON COLUMN entity_mapping.match_method IS
    'Method used for resolution (e.g., "inchikey_match", "name_match", "fingerprint_similarity").';
COMMENT ON COLUMN entity_mapping.match_history IS
    'Full resolution attempt chain as JSON array. Each entry: {method, confidence, timestamp}. '
    'Enables auditing and improvement of the resolution algorithm (LINE-03, LINE-04).';
COMMENT ON COLUMN entity_mapping.last_matched_at IS
    'Timestamp of the last entity resolution attempt on this record (LINE-04).';
COMMENT ON COLUMN entity_mapping.created_at IS
    'Record creation timestamp.';
COMMENT ON COLUMN entity_mapping.updated_at IS
    'Record last-update timestamp. Auto-updated by trigger.';


-- #####################################################################
-- TABLE 7. PIPELINE RUNS — ETL audit log
-- #####################################################################
-- WHY this table: Every pipeline execution must be auditable. This table
--   tracks what ran, when, how many records were processed, and whether
--   it succeeded. The Airflow scheduler uses this for retry logic and
--   the team uses it for debugging pipeline failures at 3 AM.
-- WHY partial failure tracking: Pipelines download 50K records, clean
--   45K, then crash. Without records_failed/records_skipped, the
--   information that 45K were cleaned is lost. The Airflow retry
--   mechanism uses last_checkpoint to resume from the last successful
--   stage instead of restarting from scratch.
-- Data sources: All 7 pipelines (ChEMBL, DrugBank, UniProt, STRING,
--   DisGeNET, OMIM, PubChem)
-- Key constraints: source enum, status enum, duration non-negative
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating pipeline_runs table...';
END $$;

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- [DES-07, COD-05] Source must be one of the 7 known pipeline names
    source          VARCHAR(50) NOT NULL,
    run_date        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    -- [DES-05] Status constrained to known values
    status          VARCHAR(20),
    records_downloaded INTEGER,
    records_cleaned    INTEGER,
    records_loaded     INTEGER,
    -- [REL-04] Partial failure tracking — critical for Airflow retry
    records_failed     INTEGER DEFAULT 0,
    records_skipped    INTEGER DEFAULT 0,
    records_updated    INTEGER DEFAULT 0,
    -- [REL-04] Checkpoint for resumable pipelines (JSON string)
    last_checkpoint    TEXT,
    -- [SEC-04] Error message capped to prevent stack trace leakage
    error_message   VARCHAR(500),
    -- [DQ-08] Duration must be non-negative
    duration_seconds INTEGER,
    -- [LOG-02, LINE-05] Input data checksum for integrity verification
    input_file_checksum VARCHAR(128),
    -- [IDEM-03, LOG-04] Pipeline configuration hash for reproducibility
    config_hash     VARCHAR(64),
    -- [DES-06] Timestamps
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ===================== CONSTRAINTS (pipeline_runs) =====================

    -- [DES-07, COD-05] Source must be a known pipeline
    CONSTRAINT chk_pipeline_runs_source
        CHECK (source IN ('chembl', 'drugbank', 'uniprot', 'string',
                          'disgenet', 'omim', 'pubchem')),
    -- [DES-05] Status enum
    CONSTRAINT chk_pipeline_runs_status
        CHECK (status IS NULL OR status IN ('running', 'success', 'failed', 'partial')),
    -- [DQ-08] Duration non-negative
    CONSTRAINT chk_pipeline_runs_duration_nonneg
        CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    -- [REL-04] Record counts non-negative
    CONSTRAINT chk_pipeline_runs_counts_nonneg
        CHECK (
            (records_downloaded IS NULL OR records_downloaded >= 0)
            AND (records_cleaned IS NULL OR records_cleaned >= 0)
            AND (records_loaded IS NULL OR records_loaded >= 0)
            AND (records_failed IS NULL OR records_failed >= 0)
            AND (records_skipped IS NULL OR records_skipped >= 0)
            AND (records_updated IS NULL OR records_updated >= 0)
        ),
    -- [SEC-01] Error message length cap
    CONSTRAINT chk_pipeline_runs_error_message
        CHECK (error_message IS NULL OR LENGTH(error_message) <= 500),

    -- [DES-07] Unique constraint for idempotent pipeline runs
    CONSTRAINT uq_pipeline_runs_source_date
        UNIQUE (source, run_date)
);

-- Indexes (CMP-02: ORM naming convention)
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_source ON pipeline_runs (source);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_status ON pipeline_runs (status);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_run_date ON pipeline_runs (run_date);

-- Trigger for auto-updating updated_at
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_pipeline_runs_updated_at'
    ) THEN
        CREATE TRIGGER trg_pipeline_runs_updated_at
            BEFORE UPDATE ON pipeline_runs
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
        RAISE NOTICE 'Created trigger trg_pipeline_runs_updated_at on pipeline_runs';
    ELSE
        RAISE NOTICE 'Trigger trg_pipeline_runs_updated_at already exists, skipping';
    END IF;
END $$;

COMMENT ON TABLE pipeline_runs IS
    'ETL pipeline execution audit log. Each row records a single pipeline run — '
    'its source, status, record counts, duration, and error details. Includes '
    'partial failure tracking (records_failed, records_skipped, last_checkpoint) '
    'for Airflow retry logic. Retention: 2 years, then archived (CMP-04).';

COMMENT ON COLUMN pipeline_runs.id IS
    'Auto-incrementing identity primary key (CMP-01).';
COMMENT ON COLUMN pipeline_runs.source IS
    'Pipeline source name. Must be one of: chembl, drugbank, uniprot, string, '
    'disgenet, omim, pubchem (DES-07, COD-05).';
COMMENT ON COLUMN pipeline_runs.run_date IS
    'Date/time when this pipeline run was initiated. Part of unique constraint '
    'with source for idempotency (DES-07).';
COMMENT ON COLUMN pipeline_runs.status IS
    'Run status. Valid: running, success, failed, partial (DES-05). '
    '"partial" means some records failed but others were processed successfully.';
COMMENT ON COLUMN pipeline_runs.records_downloaded IS
    'Number of records downloaded from the source API/file.';
COMMENT ON COLUMN pipeline_runs.records_cleaned IS
    'Number of records that passed the cleaning/normalization stage.';
COMMENT ON COLUMN pipeline_runs.records_loaded IS
    'Number of records successfully loaded into the database.';
COMMENT ON COLUMN pipeline_runs.records_failed IS
    'Number of records that failed during processing (REL-04). '
    'Default 0. Critical for Airflow retry decisions.';
COMMENT ON COLUMN pipeline_runs.records_skipped IS
    'Number of records skipped by cleaning rules (e.g., below threshold) (REL-04).';
COMMENT ON COLUMN pipeline_runs.records_updated IS
    'Number of records that were upserts (existing records updated, not new inserts) (REL-04).';
COMMENT ON COLUMN pipeline_runs.last_checkpoint IS
    'JSON string tracking which stage was last completed (REL-04). '
    'Enables resumable pipelines — Airflow can retry from this checkpoint.';
COMMENT ON COLUMN pipeline_runs.error_message IS
    'Error description if the run failed. Capped at 500 chars to prevent '
    'stack trace leakage (SEC-04). Does NOT contain internal system details.';
COMMENT ON COLUMN pipeline_runs.duration_seconds IS
    'Wall-clock duration of the pipeline run in seconds. Must be non-negative (DQ-08).';
COMMENT ON COLUMN pipeline_runs.input_file_checksum IS
    'SHA-256 checksum of the input data file for integrity verification (LINE-05). '
    'If the checksum doesn''t match, the pipeline is working with different data '
    'than expected — violating the reproducibility guarantee.';
COMMENT ON COLUMN pipeline_runs.config_hash IS
    'Hash of the pipeline configuration for reproducibility tracking (IDEM-03). '
    'Ensures the same config produces the same results.';
COMMENT ON COLUMN pipeline_runs.created_at IS
    'Record creation timestamp.';
COMMENT ON COLUMN pipeline_runs.updated_at IS
    'Record last-update timestamp. Auto-updated by trigger.';


-- #####################################################################
-- TABLE 8. REJECTED RECORDS — Dead letter queue for unprocessable data
-- #####################################################################
-- WHY: When processing millions of records from 7 databases, some records
--   will inevitably be invalid. Without a dead letter queue, these
--   records are silently dropped, and there is no way to investigate
--   or correct the underlying data quality issue. The rejected_records
--   table quarantines bad data for inspection and debugging.
-- Retention: 1 year, then purged (CMP-04)
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating rejected_records table...';
END $$;

CREATE TABLE IF NOT EXISTS rejected_records (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_table    VARCHAR(50) NOT NULL,
    source_pipeline VARCHAR(50) NOT NULL,
    raw_data        TEXT NOT NULL,
    rejection_reason VARCHAR(500) NOT NULL,
    rejection_type  VARCHAR(50) NOT NULL,
    pipeline_run_id INTEGER REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- ===================== CONSTRAINTS (rejected_records) =====================

    CONSTRAINT chk_rejected_records_rejection_type
        CHECK (rejection_type IN ('constraint_violation', 'format_error',
               'duplicate', 'reference_error', 'other'))
);

-- Indexes for querying rejected records
CREATE INDEX IF NOT EXISTS ix_rejected_records_source_table
    ON rejected_records (source_table);
CREATE INDEX IF NOT EXISTS ix_rejected_records_source_pipeline
    ON rejected_records (source_pipeline);

COMMENT ON TABLE rejected_records IS
    'Dead letter queue for unprocessable records. When a record fails validation '
    'during pipeline execution, it is quarantined here with the raw data, rejection '
    'reason, and type. This prevents silent data loss and enables investigation of '
    'data quality issues. Retention: 1 year, then purged (CMP-04).';

COMMENT ON COLUMN rejected_records.id IS
    'Auto-incrementing identity primary key (CMP-01).';
COMMENT ON COLUMN rejected_records.source_table IS
    'Target table the record was intended for (e.g., "drugs", "proteins").';
COMMENT ON COLUMN rejected_records.source_pipeline IS
    'Pipeline that rejected the record (e.g., "chembl", "drugbank").';
COMMENT ON COLUMN rejected_records.raw_data IS
    'Original record data as JSON string for debugging and reprocessing.';
COMMENT ON COLUMN rejected_records.rejection_reason IS
    'Human-readable explanation of why the record was rejected.';
COMMENT ON COLUMN rejected_records.rejection_type IS
    'Categorized rejection type: constraint_violation, format_error, duplicate, '
    'reference_error, or other. Used for analytics on data quality issues.';
COMMENT ON COLUMN rejected_records.pipeline_run_id IS
    'FK to pipeline_runs.id. ON DELETE SET NULL. Links rejected record to the '
    'pipeline run that produced it for full audit traceability.';


-- #####################################################################
-- TABLE 9. AUDIT LOG — Security and compliance audit trail
-- #####################################################################
-- WHY: Pharmaceutical data platforms require an audit trail for
--   regulatory compliance (GDPR, HIPAA). The audit_log records who
--   modified what data, when, and from where. This is separate from
--   pipeline_runs (operational) and rejected_records (data quality).
-- Retention: 5 years for compliance (CMP-04)
-- #####################################################################

DO $$
BEGIN
    RAISE NOTICE 'Creating audit_log table...';
END $$;

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_name      VARCHAR(50) NOT NULL,
    operation       VARCHAR(64) NOT NULL,
    record_id       INTEGER,
    changed_by      VARCHAR(100),
    changed_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    old_values      TEXT,
    new_values      TEXT,
    -- ROOT FIX for RT-1 / RE-1 / Compound-4 ("Migration Wall"):
    -- Migration 002 INSERTs into audit_log (table_name, operation, row_count, details)
    -- to record pre/post-migration row counts and checksums. The previous
    -- audit_log schema lacked these columns, so the FIRST INSERT in
    -- migration 002 raised `column "row_count" of relation "audit_log"
    -- does not exist`, aborting the entire migration 002 transaction.
    -- The entire migration chain stalled at version 1 — every "schema fix"
    -- claimed in v9/v10/v11 was decorative.
    -- These two columns are now part of the canonical schema so migration 002
    -- (and any future migration that logs row_count + details) runs cleanly.
    row_count       INTEGER,
    details         TEXT,

    -- ===================== CONSTRAINTS (audit_log) =====================

    -- ROOT FIX: widened operation VARCHAR(20)->VARCHAR(64) and extended the
    -- CHECK whitelist to include the migration-lineage operation tokens
    -- used by migrations 002/004/005/006 (PRE_MIGRATION_*_CHECKSUM,
    -- POST_MIGRATION_*_CHECKSUM, MIGRATION_BACKFILL, etc.). The previous
    -- whitelist rejected these tokens, aborting the migration.
    --
    -- v16 ROOT FIX (RT-1 COMPLETE): the previous whitelist still omitted
    -- 4 operation tokens actually USED by migration 002's INSERTs at
    -- lines 486, 504, 529, 696, 848 — so the migration 002 transaction
    -- STILL aborted at the first non-whitelisted INSERT and the entire
    -- migration chain stalled at version 1 (Compound-4 "Migration Wall").
    -- Adding: DELETE_NULL_DISEASE_ID, DELETE_NULL_SOURCE,
    -- PRESERVED_NULL_GENE_SYMBOL, DEDUP_MIGRATION_002.
    CONSTRAINT chk_audit_log_operation
        CHECK (operation IN (
            'INSERT', 'UPDATE', 'DELETE', 'SOFT_DELETE', 'RESTORE',
            'PRE_MIGRATION_002_CHECKSUM', 'POST_MIGRATION_002_CHECKSUM',
            'PRE_MIGRATION_004_CHECKSUM', 'POST_MIGRATION_004_CHECKSUM',
            'PRE_MIGRATION_005_CHECKSUM', 'POST_MIGRATION_005_CHECKSUM',
            'PRE_MIGRATION_006_CHECKSUM', 'POST_MIGRATION_006_CHECKSUM',
            'MIGRATION_BACKFILL', 'MIGRATION_DEDUP', 'MIGRATION_CONSTRAINT',
            'BULK_OPERATION',
            -- v16: migration 002's data-lineage INSERTs
            'DELETE_NULL_DISEASE_ID',
            'DELETE_NULL_SOURCE',
            'PRESERVED_NULL_GENE_SYMBOL',
            'DEDUP_MIGRATION_002'
        ))
);

-- Indexes
CREATE INDEX IF NOT EXISTS ix_audit_log_table_name ON audit_log (table_name);
CREATE INDEX IF NOT EXISTS ix_audit_log_changed_at ON audit_log (changed_at);
CREATE INDEX IF NOT EXISTS ix_audit_log_changed_by ON audit_log (changed_by);

COMMENT ON TABLE audit_log IS
    'Security and compliance audit trail. Records all data modifications (INSERT, '
    'UPDATE, DELETE, SOFT_DELETE, RESTORE) with before/after values. Required for '
    'regulatory compliance (GDPR Art. 30, HIPAA audit requirements). '
    'Retention: 5 years (CMP-04).';

COMMENT ON COLUMN audit_log.id IS
    'Auto-incrementing identity primary key (CMP-01).';
COMMENT ON COLUMN audit_log.table_name IS
    'Name of the table that was modified (e.g., "drugs", "proteins").';
COMMENT ON COLUMN audit_log.operation IS
    'Type of operation performed. Valid: INSERT, UPDATE, DELETE, SOFT_DELETE, RESTORE.';
COMMENT ON COLUMN audit_log.record_id IS
    'ID of the affected record. Nullable for bulk operations where individual IDs '
    'are not tracked.';
COMMENT ON COLUMN audit_log.changed_by IS
    'Identity of the user or service that made the change. Nullable for automated '
    'pipeline operations where authentication is at the service level.';
COMMENT ON COLUMN audit_log.changed_at IS
    'Timestamp of the change (timezone-aware, server time).';
COMMENT ON COLUMN audit_log.old_values IS
    'JSON representation of the record before the change. NULL for INSERT operations.';
COMMENT ON COLUMN audit_log.new_values IS
    'JSON representation of the record after the change. NULL for DELETE operations.';


-- =====================================================================
-- POST-CREATION VERIFICATION BLOCK (TEST-01, TEST-04, REL-03)
-- WHY: After creating all tables, we verify they exist and have the
--   expected structure. This catches migration drift — the silent
--   killer in database management where someone manually modifies a
--   table structure and the next migration operates on an unexpected
--   schema.
-- =====================================================================

DO $$
DECLARE
    tbl TEXT;
    col_count INTEGER;
    table_count INTEGER;
BEGIN
    RAISE NOTICE '========================================';
    RAISE NOTICE 'POST-CREATION VERIFICATION';
    RAISE NOTICE '========================================';

    -- Verify all expected tables exist
    FOR tbl IN SELECT unnest(ARRAY[
        'drugs', 'proteins', 'drug_protein_interactions',
        'protein_protein_interactions', 'gene_disease_associations',
        'entity_mapping', 'pipeline_runs', 'schema_version',
        'rejected_records', 'audit_log'
    ]) LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_name = tbl AND table_schema = 'public') THEN
            SELECT COUNT(*) INTO col_count FROM information_schema.columns
                WHERE table_name = tbl AND table_schema = 'public';
            RAISE NOTICE '  [OK] Table % exists (% columns)', tbl, col_count;
        ELSE
            RAISE WARNING '  [MISSING] Table % NOT FOUND', tbl;
        END IF;
    END LOOP;

    -- Verify key CHECK constraints exist
    RAISE NOTICE 'Verifying key constraints...';
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_inchikey_format') THEN
        RAISE NOTICE '  [OK] chk_drugs_inchikey_format';
    ELSE
        RAISE WARNING '  [MISSING] chk_drugs_inchikey_format';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_drugs_max_phase') THEN
        RAISE NOTICE '  [OK] chk_drugs_max_phase';
    ELSE
        RAISE WARNING '  [MISSING] chk_drugs_max_phase';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_ppi_combined_score') THEN
        RAISE NOTICE '  [OK] chk_ppi_combined_score';
    ELSE
        RAISE WARNING '  [MISSING] chk_ppi_combined_score';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_dpi_activity_value_positive') THEN
        RAISE NOTICE '  [OK] chk_dpi_activity_value_positive';
    ELSE
        RAISE WARNING '  [MISSING] chk_dpi_activity_value_positive';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_gda_disease_id_type') THEN
        RAISE NOTICE '  [OK] chk_gda_disease_id_type';
    ELSE
        RAISE WARNING '  [MISSING] chk_gda_disease_id_type';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_pipeline_runs_source') THEN
        RAISE NOTICE '  [OK] chk_pipeline_runs_source';
    ELSE
        RAISE WARNING '  [MISSING] chk_pipeline_runs_source';
    END IF;

    -- Verify FK references are valid
    RAISE NOTICE 'Verifying foreign key references...';
    SELECT COUNT(*) INTO table_count FROM information_schema.table_constraints tc
        JOIN information_schema.referential_constraints rc
            ON tc.constraint_name = rc.constraint_name
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
            AND tc.table_schema = 'public';
    RAISE NOTICE '  [OK] % foreign key constraints verified', table_count;

    -- Verify update_updated_at() function exists
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'update_updated_at') THEN
        RAISE NOTICE '  [OK] update_updated_at() function exists';
    ELSE
        RAISE WARNING '  [MISSING] update_updated_at() function';
    END IF;

    -- Count total tables
    SELECT COUNT(*) INTO table_count FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
    RAISE NOTICE 'Total tables in public schema: %', table_count;

    RAISE NOTICE '========================================';
    RAISE NOTICE 'VERIFICATION COMPLETE';
    RAISE NOTICE '========================================';
END $$;


-- =====================================================================
-- TEST DATA SEEDING (TEST-03)
-- Uncomment for development/testing only. These test cases verify the
-- most critical constraints are actually enforced, not just declared.
-- =====================================================================

-- POSITIVE TEST CASES (should succeed):
-- INSERT INTO drugs (inchikey, name, chembl_id, drugbank_id, max_phase, is_fda_approved, molecular_weight)
--     VALUES ('BSYNRYMUTXBXSQ-UHFFFAOYSA-N', 'Aspirin', 'CHEMBL25', 'DB00945', 4, TRUE, 180.063388);
-- Expected: SUCCESS (real Aspirin InChIKey, valid phase, valid weight)

-- INSERT INTO drugs (inchikey, name, max_phase, is_fda_approved)
--     VALUES ('SYNTH-EXPERIMENTAL-COMPOUND-X1', 'Test Synthetic', 0, FALSE);
-- Expected: SUCCESS (SYNTH-prefixed InChIKey, valid phase 0)

-- INSERT INTO protein_protein_interactions (protein_a_id, protein_b_id, combined_score, source)
--     VALUES (1, 2, 850, 'string');
-- Expected: SUCCESS (valid score 0-1000, ordered pair, valid source)

-- NEGATIVE TEST CASES (should FAIL with constraint violation):
-- INSERT INTO drugs (inchikey, name, max_phase, is_fda_approved)
--     VALUES ('INVALID', 'Test', 999, TRUE);
-- Expected: FAIL (chk_drugs_inchikey_format, chk_drugs_max_phase)

-- INSERT INTO drugs (inchikey, name, is_fda_approved)
--     VALUES ('BSYNRYMUTXBXSQ-UHFFFAOYSA-N', 'A', TRUE);
-- Expected: FAIL (chk_drugs_name_min_length — name < 2 chars)

-- INSERT INTO protein_protein_interactions (protein_a_id, protein_b_id, combined_score, source)
--     VALUES (2, 1, 99999, 'string');
-- Expected: FAIL (chk_ppi_ordered — 2 > 1, chk_ppi_combined_score — 99999 > 1000)

-- INSERT INTO gene_disease_associations (gene_symbol, disease_id, disease_id_type, source)
--     VALUES ('', '', 'omim', 'disgenet');
-- Expected: FAIL (chk_gda_gene_symbol, chk_gda_disease_id — empty strings)

-- INSERT INTO pipeline_runs (source, status, duration_seconds)
--     VALUES ('unknown', 'invalid', -5);
-- Expected: FAIL (chk_pipeline_runs_source, chk_pipeline_runs_status, chk_pipeline_runs_duration_nonneg)


COMMIT;
