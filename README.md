# Podcast Data Quality & Analytics Pipeline

A local-first Python project that demonstrates repeatable quality checks for podcast-list exports. Curated podcast directories can contain duplicate IDs, missing metadata, whitespace, and invalid website URLs; this project keeps the source rows, records validation findings, checks URLs through a safety-conscious checker, and makes audit history explorable with SQL and a dashboard.

## What this project demonstrates

From a Data Analyst / Analytics Engineer perspective, the project demonstrates a small ETL-style workflow, explicit data contracts, row-level issue reporting, append-only audit snapshots, SQL metrics with stated denominators, and a local interactive reporting layer. It is a portfolio demonstration using separate synthetic fixtures and a real-source podcast metadata sample; no business impact, production coverage, or real-world accuracy is claimed.

## Status

- Day 1: project structure, input contract, synthetic sample data
- Day 2: CSV/XLSX ingestion and validation
- Day 3: safety-checked URL checking with controlled HTTP tests
- Day 4: append-only SQLite persistence
- Day 5: SQL analytics and controlled-reference evaluation
- Day 6: read-only Streamlit dashboard with plain-language findings, searchable records, and native charts
- Day 7: final QA and portfolio documentation review
- Phase 2, Step 1: append-only SQLite pipeline with scheduled GitHub Actions publication and Streamlit Community Cloud support
- Real-data fixture: standalone, safety-conscious Apple Search API/RSS collector (`data/real_podcasts.csv`); synthetic fixtures remain unchanged

## Architecture

```text
Apple Search API → guarded RSS metadata collection → schema and row validation
        → append-only SQLite snapshots → standalone SQL analyses
CSV/XLSX → ingestion → validation → optional URL checks → SQLite snapshots
        → read-only Streamlit dashboard
```

Core modules live in src/: ingestion.py, validation.py, url_checker.py, podcast_collector.py, daily_pipeline.py, database.py, evaluation.py, and dashboard_data.py. The dashboard entry point is app.py; SQL examples live in sql/; synthetic input and reference fixtures live in data/; tests live in tests/.

## Technologies

Python 3.11+, pandas, Requests, BeautifulSoup, openpyxl, built-in `sqlite3`, Streamlit, Plotly, and pytest. Dependency versions are pinned in `requirements.txt`. No cloud service, paid API, Docker, or external database is required.

## Windows setup (PowerShell)

Install Python 3.11 or newer and the Python Launcher if needed. From the project root:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks virtual-environment activation, use the environment's interpreter directly, for example:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest
```

## Run an audit

From the project root in the VS Code PowerShell terminal, activate the environment, then run the audit runner with the included synthetic CSV:

```powershell
.\.venv\Scripts\Activate.ps1
python -m src.audit_runner --input data/sample_podcasts.csv --database data/podcast_audits.sqlite
```

The runner reads the CSV, validates it, and saves a new audit snapshot. It creates the database file and schema if the path does not exist. If the file already exists, it appends only when it is a compatible Podcast Pipeline database; it refuses to alter an unrelated or unsupported database. Running the command again adds another historical run.

Website requests are **off by default**. The command above is offline and does not contact URLs in the sample. The optional `--check-websites` flag will request eligible URLs in the input using the existing safety-checked URL checker; use that only when you intend network checks. See [`docs/audit-runner.md`](docs/audit-runner.md) for details. The input contract is documented in [`docs/input-schema.md`](docs/input-schema.md).

## Collect a real podcast metadata fixture

The optional collector uses Apple's public iTunes Search API and the `feedUrl` returned by Apple. It searches ten topic terms and keeps up to 50 unique collection IDs by default. RSS requests are sequential, timeout- and size-bounded, use only HTTP(S), reject non-public DNS/IP destinations, and validate each redirect before requesting it. The RSS channel's publisher website link is stored as `website_url`; the RSS URL and Apple listing URL are never substituted. Publisher country is left blank because Apple's storefront does not establish it. DNS rebinding remains a limitation because the HTTP client resolves the host again at connection time.

From the project root, use the active virtual environment and run:

```powershell
python -m src.podcast_collector
```

This creates `data/real_podcasts.csv` and `reports/real_podcasts_collection.md`. Existing output files are never overwritten. Collection uses public network requests by design; automated tests use mocked API data and a controlled local RSS server only. See [`docs/real-dataset-collection.md`](docs/real-dataset-collection.md) for provenance and safeguards.

To audit the collected file without making website requests, use the separate database path:

```powershell
python -m src.audit_runner --input data/real_podcasts.csv --database data/real_podcast_audits.sqlite
```

The `--check-websites` option is intentionally omitted, so website checks remain disabled.

## Daily collection pipeline

Run one collection, validation, and SQLite persistence cycle manually from the project root:

~~~powershell
.\.venv\Scripts\Activate.ps1
python -m src.daily_pipeline
~~~

The default output is a new audit snapshot in data/real_podcast_audits.sqlite. The command does not write or replace either podcast CSV and does not run publisher website checks. It reports stage statuses, row/finding totals, and errors; validation findings do not mean execution failed. See docs/daily-pipeline.md for options, partial/failure behavior, and safe reruns. GitHub Actions can run this pipeline daily and publish the SQLite history; see [`docs/cloud-mvp-deployment.md`](docs/cloud-mvp-deployment.md).

## Run tests

With the virtual environment active:

```powershell
python -m pytest
```

The suite uses temporary files, mocked API responses, and controlled local HTTP fixtures. It does not contact public websites or use the collected real dataset.

## SQLite and SQL analytics

The persistence layer uses Python's standard-library SQLite. The first call to `persist_audit_run` initializes tables automatically; `initialize_database(path)` is also available directly. To use another database, pass the path to persistence functions and enter that existing path in the dashboard sidebar. See [`docs/sqlite-persistence.md`](docs/sqlite-persistence.md) for tables and inspection commands.

The standalone queries in `sql/` cover records per run, issue occurrences and affected-record rates, URL classifications, HTTP status, redirects and destinations, missing website URLs, duplicate feed IDs, and changes across runs. The issue-rate denominator is retained records; each record counts once even when it has several issues. URL-classification percentages use saved checks only; unchecked rows are not failures, and inconclusive results retain their own classification. Query details and other denominators are in [`sql/README.md`](sql/README.md) and [`docs/sql-analytics-evaluation.md`](docs/sql-analytics-evaluation.md).

Example (after a run has been saved):

```powershell
python -c "import sqlite3,pathlib; db=sqlite3.connect('data/podcast_audits.sqlite'); db.row_factory=sqlite3.Row; q=pathlib.Path('sql/record_issue_rates.sql').read_text(encoding='utf-8-sig'); print([dict(row) for row in db.execute(q)]); db.close()"
```

## Controlled evaluation

`data/reference_url_outcomes.csv` has four synthetic, labeled routes for a local fixture: ordinary 200, HTTP 404, soft-404 content under 200, and a redirect blocked by destination safety checks. `src/evaluation.py` evaluates predicted classifications against those labels and can report accuracy and a confusion matrix for those cases. Run the controlled tests with:

```powershell
python -m pytest tests/test_evaluation.py -v
```

The four cases cover specific branches only. Their agreement does not estimate accuracy on real-world or arbitrary websites. Inconclusive results remain distinct from HTTP errors. The SQLite schema does not persist evaluation reports, so the dashboard does not fabricate or display unrecorded evaluation metrics.

## Launch the dashboard

```powershell
streamlit run app.py
```

The sidebar defaults to `data/real_podcast_audits.sqlite`; another existing database can be selected. A missing real audit is reported rather than silently replaced with synthetic data. The dashboard opens SQLite in read-only mode and never changes audit history. Local mode makes no network requests; published mode retrieves only the versioned SQLite snapshot from GitHub and never runs website checks. It presents findings, searchable podcast records, saved website results, audit history, and evaluation context in plain language. See [`docs/dashboard.md`](docs/dashboard.md) for layout, definitions, and the saved real-audit findings.

For the scheduled GitHub Actions and Streamlit Community Cloud MVP, see [`docs/cloud-mvp-deployment.md`](docs/cloud-mvp-deployment.md). Published mode reads the latest SQLite snapshot from a private Git branch using a read-only GitHub token stored in Streamlit app secrets.

## Security and limitations

- Only absolute HTTP(S) URLs without embedded credentials are accepted by the checker. It rejects localhost and non-global IP destinations, vets DNS answers, and checks each redirect before requesting its destination. Requests performs a separate DNS lookup at connection time, so the implementation cannot guarantee protection against DNS rebinding. Soft-404 detection is heuristic and can produce false positives or misses. Details are in [`docs/url-checker.md`](docs/url-checker.md).
- Dashboard queries are read-only and use the existing schema. A user-selected database path is local filesystem input; do not point the app at an untrusted SQLite file.
- Audit runs are snapshots. A repeated podcast contributes again to all-history totals. Comparison matches feed IDs only when unique in both runs; duplicate and missing IDs are surfaced separately.
- Detail tables are bounded, the app has no export or bulk audit workflow, and evaluation results are not persisted.
- The controlled reference fixture and `data/sample_podcasts.csv` are synthetic. `data/real_podcasts.csv` contains public metadata collected from Apple’s Search API and publisher RSS feeds, as described in the collection report. Verify applicable source terms and attribution requirements before redistributing that dataset. No credentials or private user data are intended to be included.

Potential next improvements include persisted evaluation reports and a manual dashboard screenshot captured from clearly labeled synthetic records. The cloud schedule and snapshot publication setup are documented in [`docs/cloud-mvp-deployment.md`](docs/cloud-mvp-deployment.md).

## Portfolio and GitHub

Suggested repository name: `podcast-data-quality-pipeline`.

Suggested description: “Local-first podcast data quality pipeline with validation, safety-conscious URL checks, SQLite audit history, SQL analytics, and a read-only Streamlit dashboard.”

The dashboard is a good source for genuine screenshots after running a temporary or clearly labeled synthetic audit: Overview, Data Quality issue breakdown, Website Health with redirect details, and Audit History comparison. Do not publish local paths, private databases, or real podcast data in screenshots.



