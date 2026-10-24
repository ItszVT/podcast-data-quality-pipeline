# Daily collection and audit pipeline

## Run manually

From the project root in a VS Code PowerShell terminal, activate the project environment and run:

~~~powershell
.\.venv\Scripts\Activate.ps1
python -m src.daily_pipeline
~~~

The command collects from Apple's iTunes Search API with the existing collector, validates the returned records using src.validation, and appends an audit snapshot using src.database. The default database is data/real_podcast_audits.sqlite. The runner writes collection metadata and findings to SQLite directly; it does not create or overwrite data/real_podcasts.csv, data/sample_podcasts.csv, or either dataset report.

Publisher website checks are off by default for this local command; the scheduled cloud workflow explicitly enables the bounded website-check stage. The collector retains its existing HTTP(S)-only, DNS/IP, redirect, timeout, response-size, and request-pacing protections for Apple API and RSS retrieval. Collection may contact Apple's API and RSS feed URLs supplied by Apple; tests use mocks and local fixtures only.

## Options

- --database PATH chooses the audit database. Existing files are checked for the project's schema before collection starts.
- --limit N sets the maximum number of unique podcasts retained (default 50).
- --results-per-term N sets Apple's requested results per search term (default 10).
- --search-term TERM sets a search term; repeat it to provide several. If omitted, the collector's standard search terms are used.
- --connect-timeout-seconds N and --read-timeout-seconds N configure the existing collector timeouts.
- --max-response-bytes N sets its response-size ceiling (default 4,000,000 bytes for each Apple API and RSS response).
- --max-redirects N sets its redirect limit.
- --api-pacing-seconds N and --rss-pacing-seconds N configure request spacing.
- --run-id ID supplies an idempotency key. If that ID is already saved in the selected database, the runner refuses before making collection requests.

The 4,000,000-byte default was adopted after a controlled paired RSS experiment on the same 50 podcast IDs and in-memory resolved feed URLs. The 4 MB condition parsed 42 feeds versus 36 at 2 MB, recovering six feeds previously rejected by the size cap, with no successful-parse regressions. Peak sampled process RSS was 70.7 MiB at 4 MB versus 68.03 MiB at 2 MB. The cap remains enforced while streaming; only fully received XML is parsed. DNS/IP validation, DNS-pinned connections, strict TLS and hostname verification, redirect revalidation, request timeouts, and the RSS-stage deadline are unchanged.

For example:

~~~powershell
python -m src.daily_pipeline --database data/real_podcast_audits.sqlite --limit 50 --results-per-term 10 --search-term technology --search-term science --run-id real-collection-2026-10-24
~~~

## Summary and stage outcomes

The JSON summary reports the audit run ID, start and completion timestamps, overall and per-stage statuses, collected/validated/stored row counts, rows with findings, finding occurrences, issue counts, and stage failures.

- succeeded: collection and validation ran and the snapshot was saved. Validation findings are data-quality results and do not fail execution.
- partial: records were collected and saved, but the collector reported an API search or RSS retrieval failure. Returned records remain in the snapshot.
- failed: collection or validation failed, or persistence failed. If collection fails, the runner attempts to save a failed, zero-record run. If validation fails, it attempts to preserve the collected source records and mark the saved run failed. A persistence failure cannot be saved in that database.
- Stage failure details are listed separately in stage_failures; validation issue counts are reported separately from execution failures.

The process exit code is 0 for succeeded, 2 for partial, and 1 for failed or a preflight error. A successful persistence transaction uses the existing audit_runs, podcast_records, and data_quality_issues tables; no schema migration is required.

A preflight failure is printed as a JSON error summary to stderr. It includes the requested or generated run ID, marks collection, validation, and persistence as `not_started`, and states that no new audit row was written. Stage failures during a run are printed in the final JSON summary. If saving a failure summary also fails, both the original stage failure and the persistence failure appear in `stage_failures`; the runner does not report a successful run.

Each saved snapshot and its podcast rows, validation issues, and URL results are committed in one SQLite transaction. A database error or process interruption before commit rolls back that new snapshot, leaving earlier runs untouched. An interrupted process may not be able to print a summary, but the transaction cannot leave a partially inserted audit run.

## Reruns and history

Every normal invocation creates a new append-only snapshot, even when it finds the same podcasts. It never removes records from prior runs, merges similar shows, or interprets an absent show as removed. This is intentional because Apple search results can be partial or change between runs.

To make a manual retry safe, supply the same --run-id as the attempted execution. If a snapshot with that ID already exists, the runner refuses before collecting, so it cannot create a duplicate snapshot. A persisted failed run also reserves its ID; use a new ID to record a later attempt. The scheduled GitHub Actions publication workflow is described in [cloud-mvp-deployment.md](cloud-mvp-deployment.md).

The existing audit runner remains available for local CSV/XLSX workflows, including data/sample_podcasts.csv; it is not replaced by this collection entry point.
