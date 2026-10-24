# Day 3: Website URL checker

The checker is implemented in `src/url_checker.py` and is called with `check_website_url(url, config=None)`. It returns a `UrlCheckResult` containing the original URL, safely reached final URL, HTTP status, redirect count and chain, elapsed time, classification, evidence, optional page title, and an error reason where appropriate.

## Classifications

| Classification | Meaning |
|---|---|
| `likely_working` | A final 2xx response was received and no configured soft-404 signal was found. This does not prove that the page is relevant or correct. |
| `http_error` | The final response status is 4xx or 5xx. |
| `suspected_soft_404` | A 2xx HTML/text response contains configured error-page wording in its title or heading, or strong wording on a short page. This remains a heuristic. |
| `inconclusive` | Timeout, connection/DNS failure, unsafe destination, redirect issue, oversized response, or other ambiguous result. |
| `invalid_input` | Missing, malformed, or unsupported URL input. |

Redirects are not their own failure classification. The checker disables Requests' automatic redirects, records each hop, validates each destination before the next request, and classifies the final response. Redirect destinations that fail validation are recorded in the chain but are never requested.

## Safety controls and limits

- Only absolute HTTP and HTTPS URLs are accepted. URL credentials are rejected.
- Localhost names and any IP address that Python does not classify as globally routable are rejected. Hostnames are resolved before each request; every returned address must be globally routable.
- Each redirect is checked under the same rules. Environment proxies are disabled so an ambient proxy cannot route around the destination check.
- Connect and read timeouts, a maximum response byte count, and a redirect limit are configurable. Calls are sequential; the module creates no concurrent request workers.
- Requests performs its own name resolution when opening the connection, separate from the safety lookup. A DNS answer could change between those operations. This implementation therefore does **not** claim complete DNS-rebinding protection. It is a proportionate local portfolio checker, not a complete SSRF defense for untrusted multi-user services.
- Only a bounded portion of response content is read. Soft-404 phrases are heuristic; a false positive or missed error page is possible.

The local-server test fixture uses a private test-only monkeypatch that makes the loopback server appear globally resolved to the safety check. Production code exposes no allow-private or test-bypass setting. Other safety tests exercise the normal resolver path, and the entire suite makes no requests to public websites.

## Running the checks

From the project root with the project virtual environment active:

```powershell
python -m pytest
```

The controlled tests cover ordinary 200, HTTP 404, suspected soft 404, working and error redirects, private redirect rejection, timeout, mocked connection failure, invalid input, prohibited IP destinations, mixed public/private DNS answers, HTTPS URL acceptance at the safety-validation stage, and oversized responses both with and without `Content-Length`.

