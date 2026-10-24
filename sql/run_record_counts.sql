-- Compare the run's recorded row count with rows actually retained.
-- These counts should agree after a successful atomic persistence operation.
SELECT
    ar.audit_run_id,
    ar.input_filename,
    ar.run_status,
    ar.started_at,
    ar.completed_at,
    ar.records_processed AS reported_records_processed,
    (SELECT COUNT(*) FROM podcast_records p
     WHERE p.audit_run_id = ar.audit_run_id) AS stored_record_count
FROM audit_runs AS ar
ORDER BY ar.started_at, ar.audit_run_id;
