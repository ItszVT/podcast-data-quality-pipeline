# Input schema and validation contract

## Scope and source files

The project uses this explicit contract for synthetic podcast-list examples; no real source export was supplied. CSV and Excel inputs are read by `src/ingestion.py`. Column headers are trimmed at their outer whitespace, cells are retained as provided, and unknown source columns are kept in each record. The persistence layer serializes the original and normalized record values so input rows remain traceable.

## Input columns

| Column | Header requirement | Value handling |
|---|---|---|
| `feed_id` | Required | Imported as text so leading zeros are preserved. Validation trims it for comparison and reports blank or duplicate identifiers. |
| `title` | Required | Blank values are errors. A trimmed normalized title is retained alongside the original value. |
| `website_url` | Required | Blank values are reported, but the row is retained. A URL candidate is only trimmed; it is not assigned a scheme, host, or new destination. |
| `description` | Optional | Missing or blank values produce a warning. |
| `categories` | Optional | Free text; missing or blank values produce a warning. No taxonomy is imposed. |
| `language` | Optional | Preserved as provided; missing or blank values produce a warning. |
| `country` | Optional | Preserved as provided; missing or blank values produce a warning. |

All three required columns must be present in the header. A missing required column is a file-level schema issue; records already read are retained. `description`, `categories`, `language`, and `country` are optional columns and values. Extra columns are reported as unexpected, preserved in the ingestion result, and retained in the persisted original record JSON.

## Implemented validation

`src.validation.validate_records(records, columns)` returns a report with each original record, normalized values, row-level issues, file-level schema issues, missing optional columns, unexpected columns, and duplicate feed IDs.

- Empty/blank `feed_id` or `title` values are `missing_required_value` errors.
- Every row containing a repeated nonblank trimmed `feed_id` receives a `duplicate_feed_id` error. Duplicate rows are not dropped.
- Website URLs are checked for blank values, malformed absolute URL structure, unsupported schemes, and outer whitespace. The categories distinguish `blank_website_url`, `malformed_url`, `unsupported_url_scheme`, and `surrounding_whitespace`.
- Blank values in optional fields produce warning-severity `missing_optional_field` findings.
- One row can have multiple findings. `issue_counts` counts occurrences; `rows_with_issues` counts each row once. File-level schema findings are reported separately and make `valid_rows` zero.

The implementation does not flag duplicate website URLs and does not upsert current records. Duplicate `feed_id` remains an audit finding; Day 4 persistence stores all rows in a run snapshot.

## URL normalization and URL-check results

`normalize_website_url(value)` trims surrounding whitespace only and validates a candidate absolute URL with an HTTP or HTTPS scheme and a host. It does not add a scheme, rewrite a hostname, remove a port, or change the path, query, or fragment. The original field stays on the record; the candidate is stored as `normalized_website_url` where applicable.

`src.url_checker.check_website_url(url, config=None)` returns a `UrlCheckResult` with original and final URL, HTTP status, redirect count and chain, response time, classification, evidence, optional error reason, and optional page title. The current classifications are:

- `likely_working`: a final 2xx response was received with no configured soft-404 signal.
- `http_error`: the final response status is 4xx or 5xx.
- `suspected_soft_404`: a 2xx text/HTML response contains conservative configured error-page signals.
- `inconclusive`: timeout, connection/DNS failure, blocked destination, redirect problem, oversized response, or another ambiguous outcome.
- `invalid_input`: blank, malformed, or unsupported URL input.

Redirects are stored observations, not independent failures. See [`url-checker.md`](url-checker.md) for safety controls and their limits, and [`sqlite-persistence.md`](sqlite-persistence.md) for the persisted schema. The checker performs no work merely by importing this schema documentation or opening the dashboard.
