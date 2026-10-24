# SQLite persistence and audit history (Day 4)

The Day 4 persistence layer is local-first and uses Python's standard-library `sqlite3`. It stores one immutable snapshot for each input row in each run. It does not perform network requests or SQL analytics.

## Tables and relationships

- `audit_runs` stores a unique run ID, UTC start timestamp, optional completion timestamp, input filename, status, processed row count, optional error summary, and JSON run metadata.
- `podcast_records` stores the record as observed in that run. It includes the source row, `feed_id`, title, original website URL, normalized website URL, and deterministic JSON containing all original and normalized fields. The unique key `(audit_run_id, source_row)` prevents accidentally storing the same source row twice in one run. `feed_id` is deliberately not unique: duplicate feed IDs must remain visible as data-quality issues rather than causing records to be dropped.
- `data_quality_issues` stores each validation issue as a separate row, with category, severity, field, message, and a run association. Schema-level issues have no podcast record association but remain linked to their run.
- `url_checks` stores at most one result per input row in a run, with common classification/status/URL columns and the complete URL-check result in deterministic JSON. Failed and inconclusive checks are stored the same way as successful checks.

All run-scoped tables have foreign keys. Indexes support lookups by run, start time, status, feed ID, URL classification, and issue category. The schema version is recorded in SQLite's `user_version` field (currently 1). The run and all its child records/issues/checks are inserted inside one transaction. If any insert or serialization fails, that run's transaction rolls back. Reprocessing the same filename creates a new run; use a new run ID (generated automatically unless supplied) and existing history is not replaced.

This snapshot-oriented `podcast_records` table is the persistence form of the input contract. It replaces the initially proposed mutable current-state `podcasts` table so each run can retain duplicate, missing, and otherwise problematic source rows without requiring a valid unique identifier. Unrecognized source columns are preserved in `record_json` when the existing validation records are passed to persistence.

## Initialize and save a run

From the project root, with the virtual environment active:

```powershell
python -c "from src.ingestion import load_podcast_file; from src.validation import validate_records; from src.database import persist_audit_run; source=load_podcast_file('data/sample_podcasts.csv'); report=validate_records(source.records, source.columns); run_id=persist_audit_run('data/podcast_audits.sqlite', source.source_filename, report.records, schema_issues=report.schema_issues); print(run_id)"
```

`initialize_database(database_path)` can be called on its own to create the parent folder and tables. `persist_audit_run(database_path, input_filename, records, ...)` initializes the schema if necessary and returns its run ID. It accepts existing `ValidatedRecord` instances or compatible mappings. Pass Day 3 results with `url_check_results={source_row: result}`; each `UrlCheckResult`, including an inconclusive result, is stored without changing the checker. `run_status` accepts `running`, `succeeded`, `partial`, or `failed`. Optional `metadata`, `error_summary`, timestamps, and schema-level issues can also be supplied.

Missing values stay SQL `NULL` where the source value is `None`; empty strings remain empty strings. Original and normalized row data are both serialized so a trimmed URL never replaces the source URL.

## Inspect the database

The SQLite command-line program is optional. This Python command uses the standard library and prints recent runs and issue counts:

```powershell
python -c "import sqlite3; db=sqlite3.connect('data/podcast_audits.sqlite'); print(*db.execute('SELECT audit_run_id, input_filename, run_status, records_processed FROM audit_runs ORDER BY started_at DESC'), sep='\\n'); print(*db.execute('SELECT audit_run_id, category, COUNT(*) FROM data_quality_issues GROUP BY audit_run_id, category ORDER BY audit_run_id, category'), sep='\\n'); db.close()"
```

The tests use temporary database files and do not touch `data/podcast_audits.sqlite`.
