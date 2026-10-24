# Real Dataset Collection

## Source and collection method

The collector uses Apple's public [iTunes Search and Lookup APIs](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/) and retains up to 50 unique podcasts using `collectionId` as the stable identifier (`itunes:collection:<id>`). The daily pipeline targets 35 previously audited IDs through Lookup and 15 exploration slots across its documented topic, language-query, and storefront strata. Storefront and query labels describe sampling context only; they do not establish publisher country or podcast origin. See [daily pipeline](daily-pipeline.md) for the persistent audit workflow and sampling behavior.

For each API result with `feedUrl`, the collector retrieves only that RSS/Atom document. It maps channel-level website, description, categories, and language when available. Apple's title, collection ID, and genre metadata provide fallbacks/identifiers. Unavailable fields remain blank. It does not use the Apple listing URL or feed URL as a publisher website, and does not follow URLs from descriptions, episode entries, or arbitrary RSS content.

The collector writes `data/real_podcasts.csv` and `reports/real_podcasts_collection.md`. It refuses to overwrite either file. The generated report includes collection date, search terms, returned API result count, unique records, RSS outcomes, missing-field counts, and errors/limitations. Collection date and counts are populated when the collector actually runs.

## Safety and request limits

- Public HTTP(S) destinations only; credentials, localhost/reserved names, non-global IPv4/IPv6 addresses, DNS results containing any non-global address, and malformed URLs are rejected.
- Every redirect target is independently validated before it is requested. Redirects are manual and limited to three.
- Explicit connection/read timeouts and a 4,000,000-byte response cap apply to API and RSS responses. Bodies are streamed and rejected as soon as the cap is exceeded; incomplete responses are not parsed as valid feeds. Requests are sequential; the collector spaces API searches by three seconds and RSS fetches by 0.4 seconds.
- Environment proxy settings are disabled so a configured proxy cannot alter the destination.
- Each destination's DNS answers are validated before the request, and libcurl's per-request resolve override pins the connection to an approved public IP while retaining the original hostname for the Host header and TLS certificate/hostname verification. Redirect destinations are independently resolved and validated before connection; proxy inheritance and automatic redirects are disabled.
- RSS/XML/API failures are recorded and do not discard the Apple listing record. XML parsing is local; no embedded link is fetched.
- No external network calls occur in the test suite. Its API behavior is mocked and its RSS behavior uses a controlled local HTTP server.

The 4,000,000-byte cap was selected after a controlled paired experiment using the same 50 podcast IDs and resolved RSS URLs under both 2,000,000-byte and 4,000,000-byte limits. The higher cap produced 42 complete RSS parses versus 36, recovered six feeds from size-limit failures, and caused no successful-parse regressions. Peak sampled process RSS was 70.7 MiB versus 68.03 MiB. These results support the bounded increase for this sample; they do not establish that a larger cap is needed or appropriate.

## Run and audit

Run from the project root after installing dependencies:

```powershell
python -m src.podcast_collector
python -m src.audit_runner --input data/real_podcasts.csv --database data/real_podcast_audits.sqlite
```

The audit command omits `--check-websites`, so it only ingests, validates, and persists the rows. URL checking is disabled by default. The audit runner will refuse an unrelated existing database rather than altering it.

## Data provenance and limitations

Apple's search results are discovery results, not a complete podcast directory. Search ranking and coverage can change; repeated runs can yield different results. RSS publishers control their feeds, so metadata may be absent, stale, or malformed. RSS fields are preferred when present, but data is not independently verified. Description/category values are copied from source metadata and may contain formatting or quality issues. Country is deliberately blank absent a reliable publisher-country source. The resulting dataset is a real-source test fixture, not a representative sample for population-level conclusions.
