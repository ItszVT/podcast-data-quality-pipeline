-- Feed IDs occurring more than once within a run. Blank IDs are excluded;
-- they are missing identifiers, not duplicates. Rows remain stored separately.
SELECT
    audit_run_id,
    feed_id,
    COUNT(*) AS duplicate_record_count
FROM podcast_records
WHERE feed_id IS NOT NULL AND TRIM(feed_id) <> ''
GROUP BY audit_run_id, feed_id
HAVING COUNT(*) > 1
ORDER BY audit_run_id, duplicate_record_count DESC, feed_id;
