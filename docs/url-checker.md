# Day 3: Website URL checker

The checker is implemented in `src/url_checker.py` and is called with `check_website_url(url, config=None)`. It returns a `UrlCheckResult` containing the requested URL, safely reached final URL, HTTP status, redirect count and chain, elapsed time, classification, evidence, optional page title, and a fixed error category where appropriate.

## Classifications

| Classification | Meaning |
|---|---|
| `likely_working` | A final 2xx response was received and no configured soft-404 signal was found. This does not prove that the page is relevant or correct. |
| `http_error` | The final response status is 4xx or 5xx. |
| `suspected_soft_404` | A 2xx HTML/text response contains configured error-page wording in its title or heading, or strong wording on a short page. This remains a heuristic. |
| `inconclusive` | Timeout, connection/DNS failure, unsafe destination, redirect issue, oversized response, or other ambiguous result. |
| `invalid_input` | Missing, malformed, or unsupported URL input. |

Redirects are handled manually, not followed automatically. Each hop is recorded, independently resolved and validated, and requested only if all destination checks pass. The final response determines the classification; a redirect alone is not a failure.

## Safety controls and limits

- Only absolute HTTP and HTTPS URLs are accepted. URL credentials are rejected. Localhost and non-global IP destinations are blocked.
- Hostnames are resolved with bounded DNS queries. Every returned IPv4/IPv6 address must be globally routable; a mixed public/private answer set is rejected.
- For each request, the checker selects an approved address and configures libcurl's per-request resolve override to pin the connection to that IP. The original hostname remains in the URL, preserving the HTTP Host header and TLS SNI/certificate hostname verification. The connection cannot silently resolve that hostname to a different address.
- Redirect destinations undergo the same DNS/IP validation and pinning before connection. Automatic redirects and environment proxy inheritance are disabled.
- Checks are sequential. The pipeline caps the stage at 20 unique websites and 90 seconds; each request attempt is limited to five seconds. Response bodies are capped at 1,000,000 bytes and at most five redirects are followed. The stage may stop before all candidates are checked when its budget is exhausted.
- Only a bounded response body is inspected. Soft-404 wording is heuristic, so false positives and missed error pages are possible. Results describe the time of a check and do not guarantee ongoing availability.

The controlled test fixtures use local HTTP servers with test-only resolver/public-address stubs. Those stubs are not exposed in production configuration. Automated tests do not contact public websites.

## Running the checks

From the project root with the project virtual environment active:

```powershell
python -m pytest tests/test_url_checker.py -q
```

The controlled tests cover ordinary 200, HTTP 404, suspected soft 404, redirects, private redirect rejection, timeout, mocked connection failures, invalid input, prohibited destinations, mixed public/private DNS answers, HTTPS certificate verification, DNS pinning, and oversized responses both with and without `Content-Length`.
