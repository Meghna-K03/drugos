-- ============================================================================
-- Drug Repurposing ETL Platform — Rollback Initial Schema
-- Migration: 001_initial_schema_rollback.sql
-- Description: Drops all tables created by 001_initial_schema.sql.
--              Safe to run multiple times (uses IF EXISTS).
--
-- v22 ROOT FIX (audit P2-10 / Section 9 — "rollback_migration raises
-- NotImplementedError"): the rollback framework in run_migrations.py
-- looks for a sidecar file named ``<migration_stem>_rollback.sql``
-- co-located with the migration. Without these sidecars,
-- ``rollback_migration(...)`` raises NotImplementedError for every
-- migration — operationally unacceptable for a 7-source ETL pipeline.
-- This file is the sidecar for 001_initial_schema.sql.
--
-- WARNING: Rolling back migration 001 DESTROYS ALL DATA in the staging
-- schema. Use only in dev/test or before a full schema rebuild. In
-- production, prefer restoring from backup.
--
-- DIALECT: PostgreSQL 15+ (primary). SQLite fallbacks are handled by
--          run_migrations.py which translates DROP statements.
-- ============================================================================

BEGIN;

-- Drop child tables first (foreign-key dependencies).
DROP TABLE IF EXISTS audit_log CASCADE;
DROP TABLE IF EXISTS rejected_records CASCADE;
DROP TABLE IF EXISTS pipeline_runs CASCADE;
DROP TABLE IF EXISTS entity_mapping CASCADE;
DROP TABLE IF EXISTS gene_disease_associations CASCADE;
DROP TABLE IF EXISTS protein_protein_interactions CASCADE;
DROP TABLE IF EXISTS drug_protein_interactions CASCADE;
DROP TABLE IF EXISTS proteins CASCADE;
DROP TABLE IF EXISTS drugs CASCADE;

-- schema_version MUST be dropped last (referenced by migration framework).
DROP TABLE IF EXISTS schema_version CASCADE;

COMMIT;
