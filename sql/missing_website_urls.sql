-- Blank includes SQL NULL, empty text, and whitespace-only text. Percentages
-- use all retained source rows in each run as the denominator.
SELECT
    ar.audit_run_id,
    COUNT(p.record_id) AS total_records,
    SUM(CASE WHEN p.record_id IS NOT NULL AND (p.website_url IS NULL OR TRIM(p.website_url) = '') THEN 1 ELSE 0 END)
        AS missing_website_urls,
    ROUND(
        100.0 * SUM(CASE WHEN p.record_id IS NOT NULL AND (p.website_url IS NULL OR TRIM(p.website_url) = '') THEN 1 ELSE 0 END)
        / NULLIF(COUNT(p.record_id), 0),
        2
    ) AS missing_website_url_pct
FROM audit_runs ar
LEFT JOIN podcast_records p ON p.audit_run_id = ar.audit_run_id
GROUP BY ar.audit_run_id
ORDER BY ar.started_at, ar.audit_run_id;

