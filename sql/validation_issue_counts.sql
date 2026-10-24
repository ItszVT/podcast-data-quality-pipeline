-- Each row is one category/severity within one run. Schema-level issues have
-- record_id NULL and therefore count as issues but affect zero individual rows.
SELECT
    i.audit_run_id,
    ar.input_filename,
    i.category,
    i.severity,
    COUNT(*) AS issue_count,
    COUNT(DISTINCT i.record_id) AS affected_record_count
FROM data_quality_issues i
JOIN audit_runs ar ON ar.audit_run_id = i.audit_run_id
GROUP BY i.audit_run_id, ar.input_filename, i.category, i.severity
ORDER BY ar.started_at, i.audit_run_id, issue_count DESC, i.category, i.severity;
