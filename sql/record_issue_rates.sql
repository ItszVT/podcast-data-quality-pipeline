-- Denominator: retained podcast_records for each run. A row is affected if it
-- has one or more record-level issues. Multiple issues count the row once.
-- Schema-level issues are shown separately and are not record-level issues.
SELECT
    ar.audit_run_id,
    ar.input_filename,
    COUNT(DISTINCT p.record_id) AS total_records,
    COUNT(DISTINCT CASE WHEN i.issue_id IS NOT NULL THEN p.record_id END)
        AS records_with_issues,
    ROUND(
        100.0 * COUNT(DISTINCT CASE WHEN i.issue_id IS NOT NULL THEN p.record_id END)
        / NULLIF(COUNT(DISTINCT p.record_id), 0),
        2
    ) AS records_with_issues_pct,
    (SELECT COUNT(*) FROM data_quality_issues schema_issue
     WHERE schema_issue.audit_run_id = ar.audit_run_id
       AND schema_issue.record_id IS NULL) AS schema_issue_count
FROM audit_runs ar
LEFT JOIN podcast_records p ON p.audit_run_id = ar.audit_run_id
LEFT JOIN data_quality_issues i
    ON i.audit_run_id = p.audit_run_id AND i.record_id = p.record_id
GROUP BY ar.audit_run_id, ar.input_filename
ORDER BY ar.started_at, ar.audit_run_id;

