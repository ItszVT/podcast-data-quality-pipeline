# Real Dataset Collection

## Source and collection method

The collector uses Apple's public [iTunes Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/) at `https://itunes.apple.com/search`, searching for podcasts in the US storefront with these terms: technology, science, business, education, news, comedy, health, history, true crime, and culture. It retains up to 50 unique podcasts using `collectionId` as the stable identifier (`itunes:collection:<id>`). The storefront parameter defines the catalog searched; it is not treated as the publisher's country.

For each API result with `feedUrl`, the collector retrieves only that RSS/Atom document. It maps channel-level website, description, categories, and language when available. Apple's title, collection ID, and genre metadata provide fallbacks/identifiers. Unavailable fields remain blank. It does not use the Apple listing URL or feed URL as a publisher website, and does not follow URLs from descriptions, episode entries, or arbitrary RSS content.

The collector writes `data/real_podcasts.csv` and `reports/real_podcasts_collection.md`. It refuses to overwrite either file. The generated report includes collection date, search terms, returned API result count, unique records, RSS outcomes, missing-field counts, and errors/limitations. Collection date and counts are populated when the collector actually runs.

## Safety and request limits

- Public HTTP(S) destinations only; credentials, localhost/reserved names, non-global IPv4/IPv6 addresses, DNS results containing any non-global address, and malformed URLs are rejected.
- Every redirect target is independently validated before it is requested. Redirects are manual and limited to three.
- Explicit connection/read timeouts and a 2 MB response cap apply to API and RSS responses. Requests are sequential; the collector spaces API searches by three seconds and RSS fetches by 0.4 seconds.
- Environment proxy settings are disabled so a configured proxy cannot alter the destination.
- DNS resolution is checked immediately before requesting, but `requests` resolves the hostname again while connecting. DNS rebinding between those operations remains possible; this implementation does not claim to eliminate that race.
- RSS/XML/API failures are recorded and do not discard the Apple listing record. XML parsing is local; no embedded link is fetched.
- No external network calls occur in the test suite. Its API behavior is mocked and its RSS behavior uses a controlled local HTTP server.

## Run and audit

Run from the project root after installing dependencies:

```powershell
python -m src.podcast_collector
python -m src.audit_runner --input data/real_podcasts.csv --database data/real_podcast_audits.sqlite
```

The audit command omits `--check-websites`, so it only ingests, validates, and persists the rows. URL checking is disabled by default. The audit runner will refuse an unrelated existing database rather than altering it.

## Data provenance and limitations

Apple's search results are discovery results, not a complete podcast directory. Search ranking and coverage can change; repeated runs can yield different results. RSS publishers control their feeds, so metadata may be absent, stale, or malformed. RSS fields are preferred when present, but data is not independently verified. Description/category values are copied from source metadata and may contain formatting or quality issues. Country is deliberately blank absent a reliable publisher-country source. The resulting dataset is a real-source test fixture, not a representative sample for population-level conclusions.
