-- Redirect counts are extracted from the persisted UrlCheckResult JSON.
-- A redirect is an observation attribute, not a URL-check failure by itself.
SELECT
    ar.audit_run_id,
    COUNT(u.url_check_id) AS checked_records,
    SUM(CASE WHEN COALESCE(json_array_length(u.result_json, '$.redirect_chain'), 0) > 0
             THEN 1 ELSE 0 END) AS records_with_redirects,
    SUM(COALESCE(json_array_length(u.result_json, '$.redirect_chain'), 0)) AS redirect_hops,
    ROUND(AVG(COALESCE(json_array_length(u.result_json, '$.redirect_chain'), 0)), 2)
        AS avg_redirect_hops_per_checked_record
FROM audit_runs ar
LEFT JOIN url_checks u ON u.audit_run_id = ar.audit_run_id
GROUP BY ar.audit_run_id
ORDER BY ar.started_at, ar.audit_run_id;
