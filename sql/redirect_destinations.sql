-- JSON1 expands each stored redirect hop. Destinations are grouped by exact
-- destination URL; this avoids guessing domain/path patterns in SQL.
SELECT
    u.audit_run_id,
    json_extract(hop.value, '$.source_url') AS source_url,
    CAST(json_extract(hop.value, '$.status_code') AS INTEGER) AS redirect_status,
    json_extract(hop.value, '$.destination_url') AS destination_url,
    COUNT(*) AS observed_hops
FROM url_checks u,
     json_each(u.result_json, '$.redirect_chain') AS hop
GROUP BY u.audit_run_id, source_url, redirect_status, destination_url
ORDER BY u.audit_run_id, observed_hops DESC, destination_url;
