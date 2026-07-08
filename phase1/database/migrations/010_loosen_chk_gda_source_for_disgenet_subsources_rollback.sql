-- Rollback for migration 010: restore the OLD strict chk_gda_source
-- WARNING: this will cause DisGeNET inserts to FAIL again. Only use
-- this rollback if you are reverting to v40 code.

DO $$
BEGIN
    ALTER TABLE gene_disease_associations DROP CONSTRAINT IF EXISTS chk_gda_source;
EXCEPTION WHEN OTHERS THEN
    NULL;
END
$$;

ALTER TABLE gene_disease_associations
    ADD CONSTRAINT chk_gda_source
    CHECK (source IS NULL OR source = 'omim' OR source = 'disgenet'
           OR source LIKE 'disgenet|_%' ESCAPE '|');

INSERT INTO _migration_audit_log (migration_id, applied_at, status, notes)
VALUES ('010', NOW(), 'rolled_back',
        'Restored strict chk_gda_source (disgenet, omim only). '
        'WARNING: DisGeNET inserts will fail with this constraint.')
ON CONFLICT DO NOTHING;
