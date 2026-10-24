# Day 5 SQLite queries

Each `.sql` file in this directory is a standalone query for the schema in `src/database.py`. Run a file against `data/podcast_audits.sqlite` with the SQLite shell (`.read sql/<file>.sql`) or Python's `sqlite3` module. Query goals, denominators, date handling, and limitations are documented in [`../docs/sql-analytics-evaluation.md`](../docs/sql-analytics-evaluation.md).

Files include run row counts, issue totals and rates, URL outcome and HTTP status distributions, redirects and exact destinations, missing website URLs, duplicate feed IDs, classification trends, and run-over-run comparisons. Inconclusive URL checks and checks without an HTTP response remain distinct.
