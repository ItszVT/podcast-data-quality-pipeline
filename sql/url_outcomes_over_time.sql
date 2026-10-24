-- Compare each observed classification over completed audit runs. Missing classes
-- in a run are zero; a run with no checks has NULL percentage, not 0%.
WITH run_checks AS (
    SELECT ar.audit_run_id, ar.started_at,
           COUNT(u.url_check_id) AS checked_records
    FROM audit_runs ar
    LEFT JOIN url_checks u ON u.audit_run_id = ar.audit_run_id
    GROUP BY ar.audit_run_id
), classes AS (
    SELECT DISTINCT classification FROM url_checks
), counts AS (
    SELECT audit_run_id, classification, COUNT(*) AS classification_count
    FROM url_checks
    GROUP BY audit_run_id, classification
), grid AS (
    SELECT r.audit_run_id, r.started_at, r.checked_records, c.classification,
           COALESCE(n.classification_count, 0) AS classification_count
    FROM run_checks r
    CROSS JOIN classes c
    LEFT JOIN counts n
      ON n.audit_run_id = r.audit_run_id AND n.classification = c.classification
), rates AS (
    SELECT *, ROUND(100.0 * classification_count / NULLIF(checked_records, 0), 2)
        AS classification_pct
    FROM grid
)
SELECT audit_run_id, started_at, strftime('%Y-%m', started_at) AS run_month,
       classification, checked_records, classification_count, classification_pct,
       LAG(classification_pct) OVER (
           PARTITION BY classification ORDER BY started_at, audit_run_id
       ) AS previous_run_pct,
       ROUND(classification_pct - LAG(classification_pct) OVER (
           PARTITION BY classification ORDER BY started_at, audit_run_id
       ), 2) AS change_from_previous_run_percentage_points
FROM rates
ORDER BY started_at, audit_run_id, classification;
