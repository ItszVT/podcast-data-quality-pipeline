-- Status denominators are all URL-check observations, including checks that
-- returned no HTTP response; those are labeled no_http_response, not dropped.
WITH per_run AS (
    SELECT audit_run_id, COUNT(*) AS total_url_checks
    FROM url_checks
    GROUP BY audit_run_id
)
SELECT
    ar.audit_run_id,
    COALESCE(CAST(u.http_status_code AS TEXT), 'no_http_response') AS status_bucket,
    COUNT(*) AS check_count,
    per_run.total_url_checks,
    ROUND(100.0 * COUNT(*) / NULLIF(per_run.total_url_checks, 0), 2) AS checks_pct
FROM url_checks u
JOIN audit_runs ar ON ar.audit_run_id = u.audit_run_id
JOIN per_run ON per_run.audit_run_id = u.audit_run_id
GROUP BY ar.audit_run_id, status_bucket, per_run.total_url_checks
ORDER BY ar.started_at, ar.audit_run_id, status_bucket;
