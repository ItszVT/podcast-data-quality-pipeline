# Real Podcast Dataset Collection Report

> **Historical transport note:** This report records the behavior of the collector used for the 2026-10-24 collection. Its DNS lookup description below is historical and does not describe the current collector, which pins each request to a validated IP while retaining hostname and TLS verification. See [the current URL checker documentation](../docs/url-checker.md). The collection counts and findings below are preserved as recorded.

- Collection date: 2026-10-24
- Source: Apple iTunes Search API and publisher RSS channel metadata
- API endpoint: `https://itunes.apple.com/search`
- Search catalog: US search catalog (not treated as publisher country)
- Search terms: technology podcast, science podcast, business podcast, education podcast, news podcast, comedy podcast, health podcast, history podcast, true crime podcast, culture podcast
- API result instances received: 59
- Unique records retained: 50
- RSS retrieval: 37 succeeded, 12 failed, 1 had no feed URL

## Missing field counts

- `feed_id`: 0
- `title`: 0
- `website_url`: 13
- `description`: 13
- `categories`: 0
- `language`: 13
- `country`: 50

## Provenance and limitations

- Search coverage depends on selected terms and the US Apple storefront; it is not a complete directory.
- Publisher country is blank because storefront country does not establish publisher location.
- Only API-provided feedUrl values are fetched. RSS website links supply publisher website metadata; feed URLs are never substituted as websites.
- RSS requests are sequential, response-size limited, timeout bounded, and every redirect is checked against public IP destinations.
- **Historical transport behavior for this collection:** DNS was checked immediately before connection, but the HTTP library resolved again; DNS rebinding between validation and connection remained possible in that implementation.
- Descriptions and arbitrary RSS links are text only and are never fetched.

## RSS retrieval errors

- itunes:collection:1522960417: RSS response exceeded the configured size limit.
- itunes:collection:120329020: RSS response exceeded the configured size limit.
- itunes:collection:1278815517: RSS response exceeded the configured size limit.
- itunes:collection:325404506: RSS response exceeded the configured size limit.
- itunes:collection:1291423644: RSS response exceeded the configured size limit.
- itunes:collection:928933489: RSS response exceeded the configured size limit.
- itunes:collection:1559120677: RSS response exceeded the configured size limit.
- itunes:collection:1639838828: RSS response exceeded the configured size limit.
- itunes:collection:730188152: RSS response exceeded the configured size limit.
- itunes:collection:1665219519: RSS response exceeded the configured size limit.
- itunes:collection:425179503: RSS response exceeded the configured size limit.
- itunes:collection:152016440: RSS response exceeded the configured size limit.
