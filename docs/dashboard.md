# Podcast Data Quality dashboard

## Run locally

From the repository root with the project virtual environment active:

```powershell
streamlit run app.py
```

The database field selects an existing local audit database. The dashboard defaults to `data/real_podcast_audits.sqlite` and reports a missing real audit instead of silently falling back to synthetic data. A local missing database is never created. In published mode, the app fetches the latest SQLite snapshot from the private `audit-snapshots` Git branch using a read-only GitHub token in app secrets. The remote snapshot is deserialized into an in-memory SQLite connection and is not written to the app filesystem. **Refresh saved data** reloads the view. SQLite connections are read-only; dashboard actions do not write records or run website checks. See [cloud-mvp-deployment.md](cloud-mvp-deployment.md) for Streamlit Community Cloud setup.

## Page structure

- **Overview** — selected audit status and file/date context, five useful metrics, findings summary, and a clear website-check empty state.
- **What needs attention** — findings grouped by category and field, with saved severity, affected-record counts, plain-language meaning, next steps, and expandable affected records. File-level findings are separated from podcast records.
- **Podcast records** — search and filters for all records, records with/without findings, missing publisher websites, or a saved finding category. Selecting a record shows its saved fields and findings. The table hides internal database columns and is read-only.
- **Website checks** — saved outcomes, response codes, redirects, and optional technical evidence. With no checks, the page says “No websites checked yet” and explicitly explains this is not a failure. It does not infer website health from missing observations.
- **Audit history** — saved snapshots and a trend only when at least two audits exist; pairwise matching uses unique nonblank podcast IDs and does not invent change explanations.
- **Evaluation** — describes automated tests, controlled reference cases, and real-world checks separately. Evaluation results are not persisted, so no accuracy score or confusion matrix is shown.

## Metric definitions

- **Records reviewed** is the number of podcast records saved for the selected audit.
- **Records needing attention** counts distinct records with at least one saved record-level finding. A record with several findings counts once.
- **Total findings** counts finding occurrences, including file-level findings. It can exceed the number of affected records.
- **Missing publisher websites** counts null, empty, or whitespace-only source website values. RSS feed URLs are not publisher websites.
- **Website checks completed** counts saved check observations. Unchecked records are neither successful nor failed.
- Finding severity is the value stored by the existing validation rules (`error`, `warning`, or `info`). The dashboard does not alter validation rules.

## Findings in the saved real-data audit

The generated `data/real_podcast_audits.sqlite` currently contains one completed audit of `real_podcasts.csv` (audit ID `9feb0753-8dc3-491c-92fc-75e40b4979e5`). The actual saved snapshot contains:

| Measure | Saved value |
|---|---:|
| Records reviewed | 50 |
| Records with at least one finding | 50 |
| Finding occurrences | 89 |
| Missing publisher websites | 13 |
| Duplicate nonblank podcast IDs | 0 |
| Website checks | 0 |

The two stored finding categories are:

| Finding category | Severity | Occurrences | Records affected |
|---|---|---:|---:|
| Missing optional field — publisher country | Warning | 50 | 50 |
| Missing optional field — description | Warning | 13 | 13 |
| Missing optional field — language | Warning | 13 | 13 |
| Blank publisher website | Error | 13 | 13 |

The three field-level optional-field counts roll up to 76 `missing_optional_field` occurrences; adding 13 `blank_website_url` errors gives 89 total. Every record has at least the country warning, so all 50 have a finding, but only the 13 blank website records have an error-level finding under the current saved rules. The remaining optional metadata is blank rather than invented. The 13 missing publisher website values do not prove those websites are unavailable; the audit did not check any sites.

## Chart label investigation

The current saved podcast titles and categories do not contain `svg` or `canvas`, and neither string appears as a saved finding category. The previous charts were Plotly charts, which use browser SVG/canvas rendering elements internally. The reported words are therefore renderer/accessibility artifacts rather than podcast categories or validation results. The redesigned app uses Streamlit's native Vega-based charts for supported, nonempty datasets and does not render empty charts.

## Interpretation and limits

An audit is a saved snapshot. Trends require at least two snapshots and show aggregate counts, not causes. Website results are saved observations only; `inconclusive` is not silently converted to a failure. The four controlled reference cases are local examples for selected code paths and do not establish real-world accuracy. The dashboard never contacts public websites.
