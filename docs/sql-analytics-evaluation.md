# SQL analytics and controlled evaluation (Day 5)

Day 5 added readable SQLite queries and a small classification evaluation workflow without changing Day 4 persistence or contacting public websites in tests. The Day 6 dashboard consumes the same schema and queries; its behavior is documented in [`dashboard.md`](dashboard.md).

## SQL analyses

Run a query against `data/podcast_audits.sqlite` after at least one audit run has been persisted. Each file in `sql/` is one standalone SQLite query:

| Query file | Measure and denominator |
|---|---|
| `run_record_counts.sql` | `audit_runs.records_processed` compared with the actual count of associated `podcast_records`. |
| `validation_issue_counts.sql` | Individual issue observations by run, category, and severity. `affected_record_count` is distinct records; schema-level issues have no record and count as zero affected rows. |
| `record_issue_rates.sql` | Rows with at least one record-level issue divided by all retained records in that run. Each affected row is counted once regardless of issue count. Schema-level issue count is separate. A run with no records has a NULL percentage. |
| `url_outcomes_by_classification.sql` | Outcomes divided by URL-check observations in that run. `inconclusive` and `invalid_input` are separate classifications. |
| `http_status_distribution.sql` | HTTP status grouped per run; checks without an HTTP response remain in a `no_http_response` bucket. The denominator is all stored URL checks. |
| `redirect_frequency.sql` | Checked records with redirects, total hops, and average hops per checked record, read from the JSON `redirect_chain`. Redirects are observations, not failures. |
| `redirect_destinations.sql` | Observed source, status, and exact destination URL counts from JSON. Grouping exact URLs avoids assuming a URL path or domain pattern. |
| `missing_website_urls.sql` | NULL, empty, or whitespace-only website fields divided by all retained rows in each run. |
| `duplicate_feed_ids.sql` | Feed IDs with multiple rows within a run. Blank IDs are excluded because missing identity is not a duplicate. |
| `url_outcomes_over_time.sql` | Per-run classification counts and percentages, plus percentage-point change from the prior run for each class. Runs with no URL checks have NULL percentages. |
| `audit_run_comparison.sql` | Per-run record/issue/check measures and changes from the preceding run ordered by timestamp and run ID. Issue rate uses retained records; URL counts remain separate. |

`strftime('%Y-%m', started_at)` supplies a calendar-month grouping for the ISO timestamps stored by Day 4. Comparison queries order by `started_at, audit_run_id` for deterministic ties. The queries use correlated subqueries or pre-aggregated counts where needed to avoid multiplying rows when issues and checks are joined.

### Execute a query

The SQLite command-line shell is optional. If installed, from the project root:

```text
sqlite3 data/podcast_audits.sqlite
.read sql/record_issue_rates.sql
```

Or run a single query with Python's built-in SQLite module:

```powershell
python -c "import sqlite3,pathlib; db=sqlite3.connect('data/podcast_audits.sqlite'); db.row_factory=sqlite3.Row; q=pathlib.Path('sql/record_issue_rates.sql').read_text(encoding='utf-8-sig'); print([dict(row) for row in db.execute(q)]); db.close()"
```

The redirect analyses use SQLite JSON functions (`json_extract`, `json_array_length`, and `json_each`) to read the Day 4 `UrlCheckResult` JSON. They do not require Python packages beyond the existing stack. Query results represent only checks and records that were persisted; a missing check is not counted as a failed check.

## Evaluation methodology

`data/reference_url_outcomes.csv` contains four labeled paths for a controlled local HTTP fixture: ordinary 200, HTTP 404, soft-404 content under HTTP 200, and a redirect toward a prohibited loopback address. `src/evaluation.py` loads those expected labels, calls the existing `check_website_url` component sequentially, and returns case-level evidence, accuracy, and a confusion matrix. The loopback base URL is validated before loading cases; the CSV accepts only local paths. `inconclusive` stays its own class and is never translated into `http_error`.

Run the end-to-end controlled evaluation as part of the test suite:

```powershell
python -m pytest tests/test_evaluation.py -v
```

The test binds a temporary server to `127.0.0.1`; a narrowly scoped test DNS seam permits only that fixture listener through the URL checker's safety lookup. The evaluator does not provide a production safety bypass. No public website is contacted. The four reference labels are designed to exercise known branches, so their accuracy measures fixture agreement only. It is not an estimate of real-world website classification performance. Confusion-matrix cells count these labeled cases, not a representative population.

To consume the evaluator result in Python, `EvaluationReport.to_dict()` returns JSON-ready case results, the accuracy ratio, and the confusion matrix. Accuracy is `None` when there are no labeled cases.

## Limitations

- Analyses use persisted snapshots. If an input run was never saved, it is absent from query results.
- `records_processed` is the number of snapshots included in the saved run; it does not represent every row in a file if an earlier stage did not pass those rows to persistence.
- Percentage denominators are stated with each query. Schema-level findings are not attached to a podcast record, so they do not increase the record issue percentage.
- URL outcomes are observations at check time. An inconclusive result may arise from timeout, DNS/network failure, safety rejection, or another ambiguity; it is not proof that a website is broken.
- Run-to-run changes can reflect changed input files, coverage, or website state. These queries do not attribute causes.
- The reference evaluation is small and synthetic, and does not establish accuracy on arbitrary public sites.
- SQLite JSON functions are required for redirect analyses. The project environment's SQLite build was exercised by automated query tests.

