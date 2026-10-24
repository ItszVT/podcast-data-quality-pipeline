# Podcast Data Quality dashboard

## Data source and behavior

The dashboard reads saved SQLite audit snapshots in read-only mode. Local mode opens an existing database (default: `data/real_podcast_audits.sqlite`); a missing database is reported and never created or replaced with synthetic data. Published mode retrieves the latest database from the private `audit-snapshots` branch through GitHub's authenticated Contents API using a read-only token in Streamlit secrets. The snapshot is validated and loaded into an in-memory SQLite connection. The dashboard does not collect podcasts, contact publisher websites, or write audit history. **Refresh saved data** reloads the published snapshot.

The selected audit context remains visible throughout navigation: execution status, record count, source/storefront context, full audit ID, start and completion times in local time, and exact UTC timestamps in an expander. A partial status means some collection requests did not complete; it is separate from validation findings and persistence status. Saved records are retained, while the coverage warning explains what is incomplete.

## Sections

- **Overview** — execution status, records analyzed, actionable errors, completeness notes, tested website targets, RSS retrieval coverage, website coverage, and an action-needed summary. Metrics apply to the selected audit.
- **Data quality** — validation results grouped into actionable errors, optional-field completeness notes, other warnings, and information. Missing publisher country is not presented as an error; unknown country is retained unless reliable evidence supports a claim.
- **Website health** — saved per-record outcomes, HTTP responses, redirect observations, and coverage. No saved checks produces a clear empty state; unchecked websites are not counted as successful or failed. A possible soft-404 is a heuristic classification, not proof that a site is unavailable.
- **Geo intelligence** — evidence availability, structured creator and subject locations, declared and observed language evidence, supported conflicts, historical changes, and regional website observations. Apple storefront is catalog context only. Unknown origin, first observation, unavailable evidence, and not-tested regions remain distinct.
- **Podcast records** — searchable records with filters for findings and missing websites, plus an expandable view of saved fields, source evidence, website observations, and geographic/language details.
- **History** — saved audits, status and sample size, record-level error rates, collection size trends when at least two eligible audits exist, and comparisons between selected runs. Missing from a later sample does not mean a podcast was removed.

The sidebar's **Methodology / About these checks** expander describes execution status, URL-check limits, geographic inference boundaries, controlled test evidence, and evaluation limits. These are details within the dashboard, not a seventh navigation section.

## Metric definitions and interpretation

- **Podcasts analyzed** is the number of records persisted for the selected audit.
- **Records with actionable errors** counts distinct selected-audit podcast records with at least one error-severity finding. File-level errors are listed separately.
- **Completeness notes** count optional-field omissions. Missing descriptions, languages, or publisher country are not proof of invalid data. When RSS retrieval failed, missing RSS-derived metadata is not treated as confirmed absence; older audits may lack enough information to distinguish unavailable from unrecorded.
- **Website checks** distinguish unique targets from per-record result rows where both are available. Percentages use the displayed denominator; unchecked records are not failures.
- **History error rate** is records with at least one error-severity finding divided by records saved in that run. Failed and in-progress runs remain visible in history but are excluded from trend lines.
- **Geographic drift** counts a change only when comparable evidence has different values. A first observation is not a change, and a missing or unavailable current observation does not erase the previous value.
- **Regional website reliability** remains “Not tested” unless an authorized probe reports a verified exit country. The normal runner's location is not used as regional evidence.

All tables and metrics are scoped to the selected audit unless explicitly labeled as a comparison or history trend. Legacy audits remain viewable; where a field or source-availability marker was not stored, the dashboard reports it as legacy, unknown, or unavailable rather than inventing a value.

## Evidence limits

Apple Search and Lookup provide catalog metadata, and RSS supplies publisher-controlled metadata when retrieval and parsing succeed. Storefront, language, category, and domain are not evidence of podcast origin. Geographic claims retain provenance and confidence; insufficient evidence remains unknown. Episode language is compared with declared language only when enough usable episode text is available, and raw episode text is not stored.

URL outcomes are bounded observations. The checker validates public DNS answers and pins each request to an approved IP while retaining the original hostname for HTTP Host and TLS verification. Redirects are revalidated. HTTP errors, possible soft-404 content under HTTP 200, and inconclusive network results are separate classifications. They do not establish broad or permanent site availability.

Automated and controlled tests exercise defined code paths. They are not a representative sample of websites, podcasts, or global catalog coverage, and the dashboard does not claim a real-world accuracy score.
