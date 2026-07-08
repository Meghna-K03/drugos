-- ============================================================================
-- Drug Repurposing ETL Platform — PipelineRun.metadata_json Column Migration
-- Migration: 007_pipeline_run_metadata.sql
-- Description: Add a `metadata_json` JSON column to the `pipeline_runs` table
--              so the rich per-run audit metadata that BasePipeline already
--              computes (run_id, correlation_id, triggered_by, source_version,
--              sha256_raw, sha256_cleaned, git_commit, seed, schema_version,
--              validation_errors, dq_metrics, record counts) is persisted
--              alongside the existing fixed columns instead of being silently
--              discarded by the PipelineRun(...) constructor.
--
-- ROOT-CAUSE FIX (audit P1-18):
--   BasePipeline._write_run_log (phase1/pipelines/base_pipeline.py) builds a
--   `metadata_json` dict containing run_id / sha256 / dq_metrics / git_commit /
--   seed / schema_version / validation_errors and passes it to _write_run_log.
--   But the PipelineRun ORM model did not declare a `metadata_json` column,
--   and the PipelineRun(...) constructor calls at lines ~4338 and ~4475 did
--   not pass `metadata_json=...`. The metadata was therefore silently
--   dropped on every successful run — operators could not query the DB for
--   "which git commit produced this run" or "what was the raw-input SHA-256"
--   without trawling log files.
--
-- This migration adds the column; the ORM and constructor are updated in
-- parallel (database/models.py, base_pipeline.py).
--
-- All new columns are NULLABLE — existing rows and existing tests are
-- unaffected. No columns are dropped, no constraints are weakened.
--
-- PREREQUISITES: 001_initial_schema.sql through 006_drug_withdrawn_safety_columns.sql.
-- Dialects: PostgreSQL (JSONB) and SQLite (JSON via TEXT).
-- ============================================================================

BEGIN;

-- ===========================================================================
-- Phase 1: Add metadata_json column
-- ===========================================================================
-- Use JSONB on PostgreSQL (binary JSON, indexable, de-duplicates keys) and
-- fall back to TEXT on SQLite (SQLite has no native JSON column type; the
-- SQLAlchemy JSON dialect serialises Python dicts to TEXT transparently).
-- The IF NOT EXISTS guard makes this safe to re-run on both dialects.
DO $$
BEGIN
    -- PostgreSQL path: JSONB column.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'pipeline_runs'
          AND column_name = 'metadata_json'
    ) THEN
        RAISE NOTICE '  [SKIP] column pipeline_runs.metadata_json already exists';
    ELSE
        BEGIN
            ALTER TABLE pipeline_runs ADD COLUMN metadata_json JSONB;
            RAISE NOTICE '  [OK] Added pipeline_runs.metadata_json (JSONB)';
        EXCEPTION WHEN OTHERS THEN
            -- Fallback: this happens on SQLite (no JSONB type) — use TEXT.
            ALTER TABLE pipeline_runs ADD COLUMN metadata_json TEXT;
            RAISE NOTICE '  [OK] Added pipeline_runs.metadata_json (TEXT fallback)';
        END;
    END IF;
END $$;

-- ===========================================================================
-- Phase 2: Schema version metadata
-- ===========================================================================
INSERT INTO schema_version (version, description)
VALUES (7, 'Add metadata_json JSON column to pipeline_runs so per-run audit metadata (run_id, sha256_raw, sha256_cleaned, git_commit, dq_metrics, validation_errors) is persisted instead of silently discarded')
ON CONFLICT (version) DO NOTHING;

COMMIT;
