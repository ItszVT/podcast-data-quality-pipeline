"""Safe local collector for podcast metadata from Apple's public iTunes Search API."""
from __future__ import annotations
import argparse, csv, ipaddress, json, socket, sys, time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET
import requests

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
SEARCH_TERMS = ("technology podcast", "science podcast", "business podcast", "education podcast", "news podcast", "comedy podcast", "health podcast", "history podcast", "true crime podcast", "culture podcast")
CSV_FIELDS = ("feed_id", "title", "website_url", "description", "categories", "language", "country")
USER_AGENT = "PodcastDataQualityDatasetCollector/1.0 (local portfolio project)"

class CollectionError(RuntimeError):
    """A safe collection request could not be completed."""

@dataclass(frozen=True)
class CollectorConfig:
    max_records: int = 50
    results_per_term: int = 10
    connect_timeout_seconds: float = 4.0
    read_timeout_seconds: float = 7.0
    max_response_bytes: int = 2_000_000
    max_redirects: int = 3
    api_pacing_seconds: float = 3.0
    rss_pacing_seconds: float = 0.4

@dataclass(frozen=True)
class FeedMetadata:
    website_url: str = ""
    description: str = ""
    categories: str = ""
    language: str = ""

@dataclass(frozen=True)
class CollectionResult:
    records: tuple[dict[str, str], ...]
    report: dict[str, Any]

def _resolve_host(host: str, port: int) -> list[str]:
    """Resolve every address; HTTP's later resolution leaves a documented rebinding race."""
    try:
        return sorted({entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as exc:
        raise CollectionError(f"DNS resolution failed for {host!r}: {exc}") from exc

def _validate_public_url(url: str) -> tuple[str, str, int]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise CollectionError(f"Malformed URL: {url!r}") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise CollectionError("Only absolute HTTP and HTTPS URLs are accepted.")
    if parts.username is not None or parts.password is not None:
        raise CollectionError("URLs containing credentials are not accepted.")
    host = parts.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local")):
        raise CollectionError("Local and reserved hostnames are not accepted.")
    port = port or (443 if parts.scheme.lower() == "https" else 80)
    if port not in {80, 443}:
        raise CollectionError("Only standard HTTP and HTTPS ports are accepted.")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = _resolve_host(host, port)
    if not addresses:
        raise CollectionError(f"DNS returned no addresses for {host!r}.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise CollectionError("DNS returned an invalid IP address.") from exc
        if not ip.is_global:
            raise CollectionError(f"A non-public destination address was rejected: {address}.")
    return url, host, port

def _safe_get_bytes(url: str, config: CollectorConfig) -> tuple[bytes, str]:
    """Fetch with explicit timeouts, size ceiling, no proxy, and vetted redirects."""
    session = requests.Session()
    session.trust_env = False
    current = url
    try:
        for hop in range(config.max_redirects + 1):
            _validate_public_url(current)
            try:
                response = session.get(current, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}, timeout=(config.connect_timeout_seconds, config.read_timeout_seconds), allow_redirects=False, stream=True)
            except requests.RequestException as exc:
                raise CollectionError(f"Request failed for {current!r}: {type(exc).__name__}.") from exc
            with response:
                if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("Location"):
                    if hop >= config.max_redirects:
                        raise CollectionError("RSS redirect limit exceeded.")
                    current = urljoin(current, response.headers["Location"])
                    _validate_public_url(current)
                    continue
                if 300 <= response.status_code < 400:
                    raise CollectionError("RSS redirect had no usable Location header.")
                response.raise_for_status()
                try:
                    length = response.headers.get("Content-Length")
                    if length and int(length) > config.max_response_bytes:
                        raise CollectionError("RSS response exceeded the configured size limit.")
                except ValueError:
                    pass
                chunks, size = [], 0
                try:
                    for chunk in response.iter_content(chunk_size=16_384):
                        if chunk:
                            size += len(chunk)
                            if size > config.max_response_bytes:
                                raise CollectionError("RSS response exceeded the configured size limit.")
                            chunks.append(chunk)
                except requests.RequestException as exc:
                    raise CollectionError(f"RSS body read failed: {type(exc).__name__}.") from exc
                return b"".join(chunks), current
        raise CollectionError("RSS redirect limit exceeded.")
    finally:
        session.close()

def parse_rss_metadata(xml_content: bytes) -> FeedMetadata:
    """Extract RSS/Atom channel fields; never dereference arbitrary embedded URLs."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise CollectionError(f"RSS XML is malformed: {exc}") from exc
    channel = root.find("./channel")
    is_atom = root.tag.rsplit("}", 1)[-1].lower() == "feed"
    if channel is None and is_atom:
        channel = root
    if channel is None:
        raise CollectionError("Feed has no RSS channel or Atom feed element.")
    def tag(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1].lower()
    def find_text(*names: str) -> str:
        # Only direct channel/feed elements count; episode descriptions must not
        # be mistaken for the podcast's publisher-level description.
        for element in channel:
            if tag(element) in names:
                text = " ".join(v.strip() for v in element.itertext() if v.strip())
                if text:
                    return text
        return ""
    website = ""
    if is_atom:
        for element in channel:
            if tag(element) == "link" and element.attrib.get("rel", "alternate") == "alternate":
                website = element.attrib.get("href", "").strip()
                if website:
                    break
    else:
        website = find_text("link")
    categories = []
    for element in channel:
        if tag(element) in {"category", "keywords"}:
            value = element.attrib.get("text", "") or element.attrib.get("term", "") or (element.text or "")
            if value.strip():
                categories.append(value.strip())
    return FeedMetadata(website, find_text("description", "summary", "subtitle"), "; ".join(dict.fromkeys(categories)), find_text("language"))

def fetch_rss_metadata(feed_url: str, config: CollectorConfig | None = None) -> FeedMetadata:
    content, _ = _safe_get_bytes(feed_url, config or CollectorConfig())
    return parse_rss_metadata(content)

def search_itunes(term: str, config: CollectorConfig | None = None) -> list[dict[str, Any]]:
    settings = config or CollectorConfig()
    url = f"{ITUNES_SEARCH_URL}?{urlencode({'term': term, 'entity': 'podcast', 'limit': settings.results_per_term, 'country': 'us'})}"
    content, _ = _safe_get_bytes(url, settings)
    try:
        results = json.loads(content.decode("utf-8")).get("results", [])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"Apple Search API returned invalid JSON for {term!r}.") from exc
    if not isinstance(results, list):
        raise CollectionError(f"Apple Search API returned an invalid result list for {term!r}.")
    return [item for item in results if isinstance(item, dict) and item.get("collectionId") is not None]

def collect_podcasts(config: CollectorConfig | None = None, *, api_search: Callable = search_itunes, rss_fetcher: Callable = fetch_rss_metadata, sleeper: Callable[[float], None] = time.sleep, search_terms: Iterable[str] = SEARCH_TERMS) -> CollectionResult:
    settings = config or CollectorConfig()
    terms = tuple(search_terms)
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    api_count = rss_successes = rss_failures = rss_missing = 0
    api_errors: list[str] = []
    for term_index, term in enumerate(terms):
        if term_index and settings.api_pacing_seconds:
            sleeper(settings.api_pacing_seconds)
        try:
            results = api_search(term, settings)
            api_count += len(results)
        except (CollectionError, requests.RequestException, OSError, ValueError) as exc:
            api_errors.append(f"{term}: {exc}")
            continue
        for item in results:
            identifier, title = item.get("collectionId"), item.get("collectionName")
            if identifier is None or not title:
                continue
            feed_id = f"itunes:collection:{identifier}"
            if feed_id in seen:
                continue
            seen.add(feed_id)
            genres = item.get("genres", [])
            records.append({"feed_id": feed_id, "title": str(title), "website_url": "", "description": str(item.get("description") or ""), "categories": "; ".join(dict.fromkeys(str(v) for v in genres if v)), "language": "", "country": "", "_feed_url": str(item.get("feedUrl") or "")})
            if len(records) >= settings.max_records:
                break
        if len(records) >= settings.max_records:
            break
    rss_errors: list[str] = []
    for record in records:
        feed_url = record.pop("_feed_url")
        if not feed_url:
            rss_missing += 1
            continue
        if settings.rss_pacing_seconds:
            sleeper(settings.rss_pacing_seconds)
        try:
            metadata = rss_fetcher(feed_url, settings)
        except (CollectionError, requests.RequestException, OSError, ValueError) as exc:
            rss_failures += 1
            rss_errors.append(f"{record['feed_id']}: {exc}")
            continue
        rss_successes += 1
        for field in ("website_url", "description", "categories", "language"):
            value = getattr(metadata, field)
            if value:
                record[field] = value
    missing = {field: sum(not str(row.get(field, "")).strip() for row in records) for field in CSV_FIELDS}
    report = {"collection_date": date.today().isoformat(), "source": "Apple iTunes Search API and publisher RSS channel metadata", "api_endpoint": ITUNES_SEARCH_URL, "storefront": "US search catalog (not treated as publisher country)", "search_terms": list(terms), "api_result_instances": api_count, "unique_records_retained": len(records), "rss_retrieval_successes": rss_successes, "rss_retrieval_failures": rss_failures, "missing_rss_feed_urls": rss_missing, "missing_field_counts": missing, "api_search_errors": api_errors, "rss_errors": rss_errors, "limitations": ["Search coverage depends on selected terms and the US Apple storefront; it is not a complete directory.", "Publisher country is blank because storefront country does not establish publisher location.", "Only API-provided feedUrl values are fetched. RSS website links supply publisher website metadata; feed URLs are never substituted as websites.", "RSS requests are sequential, response-size limited, timeout bounded, and every redirect is checked against public IP destinations.", "DNS is checked immediately before connection, but the HTTP library resolves again; DNS rebinding between validation and connection remains possible.", "Descriptions and arbitrary RSS links are text only and are never fetched."]}
    return CollectionResult(tuple({field: row.get(field, "") for field in CSV_FIELDS} for row in records), report)

def render_report(report: dict[str, Any]) -> str:
    lines = ["# Real Podcast Dataset Collection Report", "", f"- Collection date: {report['collection_date']}", f"- Source: {report['source']}", f"- API endpoint: `{report['api_endpoint']}`", f"- Search catalog: {report['storefront']}", f"- Search terms: {', '.join(report['search_terms'])}", f"- API result instances received: {report['api_result_instances']}", f"- Unique records retained: {report['unique_records_retained']}", f"- RSS retrieval: {report['rss_retrieval_successes']} succeeded, {report['rss_retrieval_failures']} failed, {report['missing_rss_feed_urls']} had no feed URL", "", "## Missing field counts", "", *[f"- `{name}`: {count}" for name, count in report["missing_field_counts"].items()], "", "## Provenance and limitations", "", *[f"- {item}" for item in report["limitations"]]]
    if report["api_search_errors"]:
        lines.extend(["", "## API search errors", "", *[f"- {error}" for error in report["api_search_errors"]]])
    if report["rss_errors"]:
        lines.extend(["", "## RSS retrieval errors", "", *[f"- {error}" for error in report["rss_errors"]]])
    return "\n".join(lines) + "\n"

def write_collection(result: CollectionResult, output_path: str | Path, report_path: str | Path) -> None:
    """Create new dataset/report files only; refuse to replace existing paths."""
    output, report = Path(output_path), Path(report_path)
    if output.resolve() == report.resolve():
        raise CollectionError("Dataset and report must use different paths.")
    conflicts = [str(p) for p in (output, report) if p.exists()]
    if conflicts:
        raise CollectionError("Refusing to overwrite existing path(s): " + ", ".join(conflicts))
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(result.records)
    with report.open("x", encoding="utf-8", newline="") as handle:
        handle.write(render_report(result.report))

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect a small real podcast CSV from Apple's public search API.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", default="data/real_podcasts.csv")
    parser.add_argument("--report", default="reports/real_podcasts_collection.md")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    output, report = Path(args.output), Path(args.report)
    if output.exists() or report.exists():
        parser.error("Output or report already exists; choose new paths. Existing files are never overwritten.")
    try:
        result = collect_podcasts(CollectorConfig(max_records=args.limit))
        write_collection(result, output, report)
    except (CollectionError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"dataset": str(output.resolve()), "report": str(report.resolve()), **result.report}, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())

