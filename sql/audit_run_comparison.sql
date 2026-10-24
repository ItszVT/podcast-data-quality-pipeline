-- One row per run. Denominators are retained source rows for issue rate and
-- URL-check observations for outcome counts. Inconclusive is reported explicitly.
WITH run_metrics AS (
    SELECT
        ar.audit_run_id,
        ar.input_filename,
        ar.run_status,
        ar.started_at,
        strftime('%Y-%m', ar.started_at) AS run_month,
        (SELECT COUNT(*) FROM podcast_records p
         WHERE p.audit_run_id = ar.audit_run_id) AS stored_records,
        (SELECT COUNT(*) FROM podcast_records p
         WHERE p.audit_run_id = ar.audit_run_id
           AND EXISTS (SELECT 1 FROM data_quality_issues i
                       WHERE i.audit_run_id = p.audit_run_id AND i.record_id = p.record_id))
            AS records_with_issues,
        (SELECT COUNT(*) FROM url_checks u WHERE u.audit_run_id = ar.audit_run_id)
            AS url_checks,
        (SELECT COUNT(*) FROM url_checks u WHERE u.audit_run_id = ar.audit_run_id
           AND u.classification = 'likely_working') AS likely_working_checks,
        (SELECT COUNT(*) FROM url_checks u WHERE u.audit_run_id = ar.audit_run_id
           AND u.classification = 'inconclusive') AS inconclusive_checks
    FROM audit_runs ar
), rates AS (
    SELECT *, ROUND(100.0 * records_with_issues / NULLIF(stored_records, 0), 2)
        AS records_with_issues_pct
    FROM run_metrics
)
SELECT
    audit_run_id, input_filename, run_status, started_at, run_month,
    stored_records, records_with_issues, records_with_issues_pct,
    url_checks, likely_working_checks, inconclusive_checks,
    stored_records - LAG(stored_records) OVER (ORDER BY started_at, audit_run_id)
        AS record_change_from_previous_run,
    ROUND(records_with_issues_pct - LAG(records_with_issues_pct) OVER (
        ORDER BY started_at, audit_run_id
    ), 2) AS issue_rate_change_percentage_points,
    url_checks - LAG(url_checks) OVER (ORDER BY started_at, audit_run_id)
        AS url_check_change_from_previous_run
FROM rates
ORDER BY started_at, audit_run_id;
