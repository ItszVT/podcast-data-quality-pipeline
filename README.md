# Podcast Data Quality & Analytics Pipeline

A Python portfolio project that collects real podcast catalog metadata, validates records, checks publisher websites with bounded requests, and preserves audit history for SQL analysis and a read-only Streamlit dashboard.

## What it does

- Collects up to 50 unique Apple podcast IDs per daily audit. The deterministic plan targets **35 previously observed podcasts** for longitudinal comparison and **15 exploration slots** across documented topic, language-query, and storefront strata. Apple storefront is sampling context only; it is not publisher location or podcast origin.
- Retrieves RSS metadata with sequential, bounded requests. The current response cap is **4,000,000 bytes per response**; only fully retrieved, valid XML is parsed. Missing RSS fields remain missing, and individual fetch failures are retained as partial-collection evidence.
- Validates records and saves each run as an append-only SQLite audit. Repeated podcasts remain separate observations, so history is preserved rather than overwritten.
- Runs optional publisher website checks through a security-focused URL checker. It vets every DNS answer, pins each request to an approved public IP, retains the hostname for HTTP Host and TLS certificate verification, validates redirects independently, disables proxy inheritance and automatic redirects, and applies request, response, and stage limits. HTTP failures and possible soft-404 pages are distinct evidence-based outcomes.
- Presents saved audit execution status, data-quality findings, website observations, geographic evidence, searchable records, and history in Streamlit. The dashboard reads SQLite in read-only mode and never runs collection or website requests.

## Architecture

```text
GitHub Actions (daily or manual)
  └─ Python collection → Apple Search/Lookup → bounded RSS enrichment
     → validation → SQLite audit snapshot → integrity/publication checks
        └─ versioned SQLite file on audit-snapshots branch
           └─ Streamlit Community Cloud reads the private snapshot
              through GitHub's Contents API using a read-only token

Local CSV/XLSX → Python ingestion and validation → SQLite
                                      ├─ SQL analytics
                                      └─ local read-only Streamlit dashboard
```

GitHub Actions serializes publication runs, restores the previous database, appends a new audit, validates the result, and publishes only the SQLite snapshot file. Recoverable RSS-partial audits may be published only when the established collection thresholds, validation, persistence, and SQLite integrity checks pass. Failed or inconsistent runs are not published. The dashboard never syncs or writes the snapshot branch itself.

## Implementation status

**Implemented:** Python collection, validation, secure website checks, SQLite persistence and history, SQL analyses, GitHub Actions audit/snapshot publication, and the Streamlit dashboard.

**PostgreSQL groundwork only:** optional connection configuration, strict remote TLS settings, migrations, and integration-test scaffolding are present. The production pipeline and dashboard still use SQLite; PostgreSQL persistence, migration of audit history, and dashboard support are not implemented.

## Verified collection evidence

A successful controlled GitHub Actions verification collected **35/35 repeat IDs** through Apple Lookup and **15/15 exploration IDs** through Apple Search, persisting 50 unique records. In that run, 42 of 50 RSS feeds parsed; six exceeded the 4 MB cap, one timed out, and one record had no RSS URL. The audit was therefore correctly marked partial while validation, persistence, and SQLite integrity succeeded. This is one verification run, not a promise about every production audit.

A separate paired experiment tested the same 50 IDs and resolved feed URLs at 2 MB and 4 MB. Complete RSS parses increased from 36 to 42, six feeds were recovered, and no previously successful parse regressed. Sampled peak process memory was 68.03 MiB at 2 MB and 70.7 MiB at 4 MB. These experimental results describe that sample only; they do not establish performance for every audit.

Apple search ranking, publisher-controlled RSS availability, and metadata completeness vary. The dataset is not a globally representative sample. Synthetic fixtures are kept separate from real production audits; no synthetic records are inserted into production history.

## Run locally

Python 3.11 or newer is recommended. In PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m pytest
```

Run an offline CSV audit:

```powershell
python -m src.audit_runner --input data/sample_podcasts.csv --database data/podcast_audits.sqlite
```

Run the real collection pipeline (uses public Apple and RSS endpoints):

```powershell
python -m src.daily_pipeline --database data/real_podcast_audits.sqlite
```

The daily pipeline does not check publisher websites unless explicitly run with `--check-websites`. Website checks are capped at 20 unique URLs and use the existing 90-second stage budget. Review [daily pipeline options and status semantics](docs/daily-pipeline.md) before running live collection.

Launch the dashboard:

```powershell
streamlit run app.py
```

The local dashboard opens an existing SQLite database read-only and reports missing data instead of substituting synthetic records. Published mode reads the latest database snapshot from the private `audit-snapshots` branch; setup is documented in [cloud deployment](docs/cloud-mvp-deployment.md).

## Dashboard guide

The six navigation sections are **Overview**, **Data quality**, **Website health**, **Geo intelligence**, **Podcast records**, and **History**. The **Methodology / About these checks** expander in the sidebar explains status, evidence limits, and evaluation context. The selected audit's ID, source, timestamp, and execution status remain visible across sections. See [dashboard definitions and interpretation](docs/dashboard.md).

A partial audit means collection evidence is incomplete; it does not mean saved records were discarded or that the database failed. Missing RSS metadata is distinct from a successful source check with no claim, and older audits may not contain enough source-availability detail to distinguish those cases. Website results describe only sites actually checked. Geo Intelligence reports unknown origin when evidence is insufficient; it does not infer origin from language, storefront, or domain.

## Data, SQL, and documentation

- `src/` contains ingestion, collection, validation, URL checking, persistence, pipeline orchestration, and dashboard data access.
- `sql/` contains run-scoped analytics queries and their definitions.
- `data/sample_podcasts.csv` and controlled reference cases are synthetic test fixtures. Real collected metadata is kept separate and should be redistributed only after reviewing source terms and attribution requirements.
- [Real collection provenance and safeguards](docs/real-dataset-collection.md)
- [SQLite schema and append-only history](docs/sqlite-persistence.md)
- [PostgreSQL foundation and explicit limitations](docs/postgres-foundation.md)
- [Cloud deployment and snapshot operation](docs/cloud-mvp-deployment.md)

The test suite uses temporary databases, mocked API calls, and local fixtures. It does not contact public sites or Neon. No credentials are intended to be stored in source control, SQLite snapshots, or workflow logs.
