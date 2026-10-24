# Running an audit

## VS Code on Windows

Open the project folder in VS Code, choose **Terminal → New Terminal**, and make sure the terminal is at the project root. In PowerShell, activate the existing environment and run the sample audit:

```powershell
.\.venv\Scripts\Activate.ps1
python -m src.audit_runner --input data/sample_podcasts.csv --database data/podcast_audits.sqlite
```

The command prints a JSON summary with the audit run ID, input row count, valid rows, rows with issues, issue occurrences, database path, and number of website checks saved. By default, the website-check count is zero: the command does not send requests to any URL.

The database file is initialized automatically when the selected path does not exist. If it already exists, the runner checks its project schema and version in read-only mode before continuing. It appends a new snapshot to a compatible project database. It refuses to modify an unrelated, unreadable, or unsupported database. It never deletes or replaces a database.

The dashboard defaults to `data/real_podcast_audits.sqlite` when that file exists; otherwise it uses `data/podcast_audits.sqlite`. Select the database you just audited in the sidebar when needed. After the audit completes, start the dashboard in the same terminal (or a second terminal at the project root):

```powershell
streamlit run app.py
```

The dashboard reads the selected database in read-only mode. The saved snapshot appears under **Overview**, findings under **Data quality**, saved observations under **Website health**, and records under **Podcast records**. Website health shows a clear empty state when an offline audit saved no website results. The runner does not seed or fabricate URL results.

## Optional website checks

Only use this option when you intend to request eligible HTTP(S) URLs from the input file:

```powershell
python -m src.audit_runner --input path\to\podcasts.csv --database data/podcast_audits.sqlite --check-websites
```

The existing URL checker blocks localhost and non-global addresses, pins connections to validated public IPs with libcurl, preserves TLS hostname verification, and revalidates every redirect destination. Do not use this option for the synthetic sample during the local-only first-run setup. The controlled local HTTP fixture is exercised by the URL-checker and evaluation tests instead:

```powershell
python -m pytest tests/test_url_checker.py tests/test_evaluation.py -q
```

Those tests use loopback fixtures and do not contact public websites.

## Choosing another database

Pass a different path with `--database`. A missing path is created. An existing file is only used when it has this project's schema version; incompatible files are rejected without modification. Use that same database path in the dashboard sidebar if it is not the default.
