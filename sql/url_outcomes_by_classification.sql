-- Outcome proportions use checked records (rows with one stored URL result),
-- not all input records. Inconclusive and invalid_input remain separate classes.
WITH per_run AS (
    SELECT audit_run_id, COUNT(*) AS checked_records
    FROM url_checks
    GROUP BY audit_run_id
)
SELECT
    ar.audit_run_id,
    u.classification,
    COUNT(*) AS record_count,
    per_run.checked_records,
    ROUND(100.0 * COUNT(*) / NULLIF(per_run.checked_records, 0), 2) AS checked_pct
FROM url_checks u
JOIN audit_runs ar ON ar.audit_run_id = u.audit_run_id
JOIN per_run ON per_run.audit_run_id = u.audit_run_id
GROUP BY ar.audit_run_id, u.classification, per_run.checked_records
ORDER BY ar.started_at, ar.audit_run_id, u.classification;
