"""Safe local collector for podcast metadata from Apple's public iTunes Search API."""
from __future__ import annotations
import argparse, csv, ipaddress, json, socket, sys, time
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET
from curl_cffi import CurlOpt
from curl_cffi.requests import Session as CurlSession
from curl_cffi.requests.exceptions import (
    CertificateVerifyError, ConnectionError as CurlConnectionError, DNSError,
    RequestException as CurlRequestException,
    SSLError, Timeout as CurlTimeout,
)

from .url_checker import _validated_destination

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
@dataclass(frozen=True)
class SearchStratum:
    """Documented query context; topic/language labels never describe origin."""
    label: str
    query: str
    topic: str
    language_context: str
    storefront: str
    sampling_purpose: str = "exploration"


# Fixed, reproducible exploration order: topic diversity first, followed by
# language-query/storefront contexts. Context labels are not source claims.
SEARCH_STRATA = (
    SearchStratum("technology-en-us", "technology podcast", "technology", "English query context", "us"),
    SearchStratum("science-en-gb", "science podcast", "science", "English query context", "gb"),
    SearchStratum("business-en-ca", "business podcast", "business", "English query context", "ca"),
    SearchStratum("education-en-au", "education podcast", "education", "English query context", "au"),
    SearchStratum("news-en-us", "news podcast", "news", "English query context", "us"),
    SearchStratum("comedy-en-gb", "comedy podcast", "comedy", "English query context", "gb"),
    SearchStratum("health-en-in", "health podcast", "health", "English query context", "in"),
    SearchStratum("history-en-ca", "history podcast", "history", "English query context", "ca"),
    SearchStratum("true-crime-en-au", "true crime podcast", "true crime", "English query context", "au"),
    SearchStratum("culture-en-us", "culture podcast", "culture", "English query context", "us"),
    SearchStratum("technology-hi-in", "technology podcast Hindi", "technology", "Hindi query context", "in"),
    SearchStratum("science-es-mx", "science podcast español", "science", "Spanish query context", "mx"),
    SearchStratum("history-pt-br", "history podcast português", "history", "Portuguese query context", "br"),
    SearchStratum("education-fr-fr", "education podcast français", "education", "French query context", "fr"),
    SearchStratum("culture-ja-jp", "culture podcast 日本語", "culture", "Japanese query context", "jp"),
)
DEFAULT_SEARCH_PLAN = SEARCH_STRATA
CSV_FIELDS = ("feed_id", "title", "website_url", "description", "categories", "language", "country")
USER_AGENT = "PodcastDataQualityDatasetCollector/1.0 (local portfolio project)"

class CollectionError(RuntimeError):
    """A safe collection request could not be completed."""


def _safe_error_category(error: BaseException) -> str:
    """Map fetch failures to fixed labels; never serialize exception text."""
    message = str(error).lower()
    if "dns" in message or "could not be resolved" in message:
        return "dns_failure"
    if "certificate verification" in message:
        return "tls_certificate_verification"
    if "tls" in message or "secure connection" in message:
        return "tls_connection"
    if "deadline" in message or "timed out" in message or "timeout" in message:
        return "timeout"
    if "non-public" in message or "prohibited" in message:
        return "destination_blocked"
    if "size limit" in message or "response exceeded" in message:
        return "response_size_limit"
    if "unsuccessful status" in message:
        return "http_status_failure"
    if "http" in message and ("status" in message or "error" in message):
        return "http_status_failure"
    if "redirect" in message:
        return "redirect_failure"
    if "invalid json" in message or "invalid result" in message:
        return "invalid_api_response"
    if "malformed" in message or "feed has no rss channel" in message:
        return "malformed_feed"
    if any(term in message for term in ("connection", "connect", "network")):
        return "connection_failure"
    return "request_failure"


def _rss_diagnostic_category(category: str) -> str:
    if category == "dns_failure":
        return "dns_failure"
    if category in {"tls_certificate_verification", "tls_connection", "connection_failure"}:
        return "connection_tls_failure"
    if category == "http_status_failure":
        return "http_error"
    if category == "redirect_failure":
        return "http_error"
    if category == "timeout":
        return "timeout"
    if category == "response_size_limit":
        return "response_size_limit"
    if category == "malformed_feed":
        return "xml_parsing_failure"
    return "other_or_unknown_failure"


def _apple_collection_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    text = str(value)
    return text if re.fullmatch(r"[1-9][0-9]*", text) else None

@dataclass(frozen=True)
class CollectorConfig:
    max_records: int = 50
    results_per_term: int = 10
    connect_timeout_seconds: float = 4.0
    read_timeout_seconds: float = 7.0
    max_response_bytes: int = 2_000_000
    max_redirects: int = 3
    api_pacing_seconds: float = 3.2
    rss_pacing_seconds: float = 0.4
    repeat_target: int | None = None
    exploration_target: int | None = None
    rss_collection_budget_seconds: float = 240.0
    rss_request_timeout_seconds: float = 12.0
    max_episode_dates_per_feed: int = 8
    storefront: str = "us"

    def __post_init__(self) -> None:
        if self.max_records < 1 or self.results_per_term < 1:
            raise ValueError("Collection limits must be positive.")
        if self.rss_collection_budget_seconds <= 0 or self.rss_request_timeout_seconds <= 0:
            raise ValueError("RSS time budgets must be positive.")
        if self.rss_request_timeout_seconds > 15 or self.rss_collection_budget_seconds > 600:
            raise ValueError("RSS time budgets exceed supported safety bounds.")
        if self.max_episode_dates_per_feed < 0 or self.max_episode_dates_per_feed > 20:
            raise ValueError("Episode date limit must be between zero and 20.")
        if len(self.storefront) != 2 or not self.storefront.isalpha():
            raise ValueError("Apple storefront must be a two-letter code.")
        repeat = self.repeat_target if self.repeat_target is not None else min(35, self.max_records)
        explore = self.exploration_target if self.exploration_target is not None else min(15, max(0, self.max_records - repeat))
        if repeat < 0 or explore < 0 or repeat + explore > self.max_records:
            raise ValueError("Repeat and exploration targets must fit within max_records.")

@dataclass(frozen=True)
class FeedMetadata:
    website_url: str = ""
    description: str = ""
    categories: str = ""
    language: str = ""
    episode_text_samples: tuple[str, ...] = ()
    locations: tuple[dict[str, str], ...] = ()
    location_parse_errors: int = 0
    episode_publication_dates: tuple[str, ...] = ()

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

def _safe_get_bytes(url: str, config: CollectorConfig, *, deadline_monotonic: float | None = None) -> tuple[bytes, str]:
    """Fetch over pinned libcurl with strict TLS, manual redirects, and byte/time caps."""
    current = url
    stage_deadline = deadline_monotonic or (time.monotonic() + config.rss_request_timeout_seconds)
    for hop in range(config.max_redirects + 1):
        remaining = stage_deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError("RSS collection deadline exceeded.")
        candidate, safety_error, input_error, host, approved_ip, port = _validated_destination(
            current, deadline_monotonic=stage_deadline,
            dns_timeout_seconds=min(config.connect_timeout_seconds, remaining),
        )
        if safety_error or input_error or not candidate:
            category = safety_error or input_error or "invalid_url"
            safe_categories = {
                "dns_resolution_failed": "RSS destination DNS resolution failed.",
                "prohibited_ip_destination": "RSS destination resolves to a non-public address.",
                "prohibited_hostname": "RSS destination hostname was prohibited.",
                "url_credentials_not_allowed": "RSS destination URL credentials are not allowed.",
                "unsupported_scheme": "RSS destination scheme is unsupported.",
                "malformed_url": "RSS destination URL is malformed.",
                "check_budget_exhausted": "RSS collection deadline exceeded.",
            }
            raise CollectionError(safe_categories.get(category, "RSS destination failed URL safety validation."))
        assert host and approved_ip and port
        remaining = stage_deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError("RSS collection deadline exceeded.")
        request_timeout = min(config.rss_request_timeout_seconds, remaining)
        body = bytearray()
        too_large = False

        def collect_chunk(chunk: bytes) -> int:
            nonlocal too_large
            if len(body) + len(chunk) > config.max_response_bytes:
                too_large = True
                return 0
            body.extend(chunk)
            return len(chunk)

        host_token = f"[{host}]" if ":" in host else host
        ip_token = f"[{approved_ip}]" if ":" in approved_ip else approved_ip
        options = {
            CurlOpt.PROTOCOLS_STR: "http,https", CurlOpt.SSL_VERIFYPEER: 1,
            CurlOpt.SSL_VERIFYHOST: 2, CurlOpt.TIMEOUT_MS: max(1, int(request_timeout * 1000)),
            CurlOpt.CONNECTTIMEOUT_MS: max(1, int(min(config.connect_timeout_seconds, request_timeout) * 1000)),
            CurlOpt.NOSIGNAL: 1,
        }
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if not literal:
            options[CurlOpt.RESOLVE] = [f"{host_token}:{port}:{ip_token}"]
        try:
            with CurlSession(
                trust_env=False, retry=0, timeout=request_timeout, allow_redirects=False,
                max_redirects=0, verify=True, proxies={"http": "", "https": ""},
                curl_options=options,
            ) as session:
                response = session.get(
                    candidate,
                    headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                    allow_redirects=False, max_redirects=0, verify=True,
                    proxies={"http": "", "https": ""}, content_callback=collect_chunk,
                )
        except CurlTimeout as exc:
            raise CollectionError("RSS request or collection deadline exceeded.") from exc
        except CertificateVerifyError as exc:
            raise CollectionError("RSS TLS certificate verification failed.") from exc
        except DNSError as exc:
            raise CollectionError("RSS DNS connection failed.") from exc
        except SSLError as exc:
            raise CollectionError("RSS TLS connection failed.") from exc
        except CurlConnectionError as exc:
            if too_large:
                raise CollectionError("RSS response exceeded the configured size limit.") from exc
            raise CollectionError("RSS connection failed safely.") from exc
        except CurlRequestException as exc:
            if too_large:
                raise CollectionError("RSS response exceeded the configured size limit.") from exc
            raise CollectionError("RSS request failed safely.") from exc
        if too_large:
            raise CollectionError("RSS response exceeded the configured size limit.")
        if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("Location"):
            if hop >= config.max_redirects:
                raise CollectionError("RSS redirect limit exceeded.")
            current = urljoin(current, response.headers["Location"])
            continue
        if 300 <= response.status_code < 400:
            raise CollectionError("RSS redirect had no usable Location header.")
        if response.status_code >= 400:
            raise CollectionError("RSS server returned an unsuccessful status.")
        return bytes(body), current
    raise CollectionError("RSS redirect limit exceeded.")

def parse_rss_metadata(xml_content: bytes, *, max_episode_dates: int = 8) -> FeedMetadata:
    """Extract RSS/Atom channel fields; never dereference arbitrary embedded URLs."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise CollectionError(f"RSS XML is malformed: {exc}") from exc
    def tag(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1].lower()
    channel = next((element for element in root if tag(element) == "channel"), None)
    is_atom = tag(root) == "feed"
    if channel is None and is_atom:
        channel = root
    if channel is None:
        raise CollectionError("Feed has no RSS channel or Atom feed element.")
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
    # Only accept the official Podcasting 2.0 namespace. Keep these publisher
    # declarations separate from the channel country and from podcast origin.
    podcast_ns = "https://podcastindex.org/namespace/1.0"
    locations: list[dict[str, str]] = []
    location_parse_errors = 0
    for element in channel:
        if element.tag != f"{{{podcast_ns}}}location":
            continue
        rel = (element.attrib.get("rel") or "subject").strip().lower()
        if rel not in {"creator", "subject"}:
            location_parse_errors += 1
            continue
        text = " ".join(value.strip() for value in element.itertext() if value.strip())[:128]
        country = (element.attrib.get("country") or "").strip()[:16]
        if text or country:
            locations.append({"rel": rel, "text": text, "country": country})
        else:
            location_parse_errors += 1
    samples: list[str] = []
    publication_dates: list[str] = []
    for episode in channel:
        if tag(episode) not in {"item", "entry"}:
            continue
        if len(publication_dates) < max_episode_dates:
            for element in episode:
                if tag(element) not in {"pubdate", "published", "updated", "date"}:
                    continue
                raw = " ".join(value.strip() for value in element.itertext() if value.strip())
                try:
                    parsed = parsedate_to_datetime(raw)
                except (TypeError, ValueError, OverflowError):
                    try:
                        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except (TypeError, ValueError, OverflowError):
                        continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                publication_dates.append(parsed.astimezone(timezone.utc).date().isoformat())
                break
        if len(samples) >= 8:
            continue
        parts = []
        remaining = 1600
        for element in episode:
            if tag(element) not in {"title", "description", "summary", "content", "encoded"}:
                continue
            text = " ".join(value.strip() for value in element.itertext() if value.strip())
            if text and remaining:
                piece = text[:remaining]
                parts.append(piece)
                remaining -= len(piece)
        sample = " ".join(parts).strip()
        if sample:
            samples.append(sample[:1600])
    return FeedMetadata(website, find_text("description", "summary", "subtitle"), "; ".join(dict.fromkeys(categories)), find_text("language"), tuple(samples), tuple(locations), location_parse_errors, tuple(publication_dates))

def fetch_rss_metadata(feed_url: str, config: CollectorConfig | None = None, *, deadline_monotonic: float | None = None) -> FeedMetadata:
    settings = config or CollectorConfig()
    content, _ = _safe_get_bytes(feed_url, settings, deadline_monotonic=deadline_monotonic)
    return parse_rss_metadata(content, max_episode_dates=settings.max_episode_dates_per_feed)

def search_itunes(term: str, config: CollectorConfig | None = None) -> list[dict[str, Any]]:
    settings = config or CollectorConfig()
    url = f"{ITUNES_SEARCH_URL}?{urlencode({'term': term, 'entity': 'podcast', 'limit': settings.results_per_term, 'country': settings.storefront.lower()})}"
    content, _ = _safe_get_bytes(url, settings)
    try:
        results = json.loads(content.decode("utf-8")).get("results", [])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CollectionError(f"Apple Search API returned invalid JSON for {term!r}.") from exc
    if not isinstance(results, list):
        raise CollectionError(f"Apple Search API returned an invalid result list for {term!r}.")
    return [item for item in results if isinstance(item, dict) and item.get("collectionId") is not None]


def lookup_itunes(collection_id: str, config: CollectorConfig | None = None) -> list[dict[str, Any]]:
    """Look up one validated Apple collection ID in the requested catalog."""
    canonical_id = _apple_collection_id(collection_id)
    if canonical_id is None:
        raise CollectionError("Apple lookup requires a numeric collection ID.")
    settings = config or CollectorConfig()
    url = f"{ITUNES_LOOKUP_URL}?{urlencode({'id': canonical_id, 'entity': 'podcast', 'country': settings.storefront.lower()})}"
    content, _ = _safe_get_bytes(url, settings)
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CollectionError("Apple Lookup API returned invalid JSON.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise CollectionError("Apple Lookup API returned an invalid result list.")
    return [item for item in payload["results"] if isinstance(item, dict)]

def collect_podcasts(config: CollectorConfig | None = None, *, api_search: Callable = search_itunes, api_lookup: Callable = lookup_itunes, rss_fetcher: Callable = fetch_rss_metadata, sleeper: Callable[[float], None] = time.sleep, search_terms: Iterable[str | SearchStratum] | None = None, previously_audited_feed_ids: Iterable[str] = (), clock: Callable[[], float] = time.monotonic) -> CollectionResult:
    settings = config or CollectorConfig()
    supplied_terms = tuple(search_terms) if search_terms is not None else DEFAULT_SEARCH_PLAN
    unique_terms: list[str | SearchStratum] = []
    seen_strata: set[str] = set()
    for term in supplied_terms:
        key = term.label if isinstance(term, SearchStratum) else str(term)
        if key not in seen_strata:
            seen_strata.add(key)
            unique_terms.append(term)
    terms = tuple(unique_terms)
    term_keys = tuple(term.label if isinstance(term, SearchStratum) else str(term) for term in terms)
    prior_ids = {str(value) for value in previously_audited_feed_ids}
    repeat_target = settings.repeat_target if settings.repeat_target is not None else min(35, settings.max_records)
    exploration_target = settings.exploration_target if settings.exploration_target is not None else min(15, max(0, settings.max_records - repeat_target))
    # Repeat selection is based only on the saved stable IDs. Exploration is
    # selected later by round-robin across configured search strata.
    queues: dict[str, list[dict[str, Any]]] = {term: [] for term in term_keys}
    seen: set[str] = set()
    api_count = lookup_result_count = api_request_count = 0
    lookup_api_call_count = search_call_count = 0
    api_errors: list[str] = []
    lookup_errors: list[str] = []
    lookup_status_by_feed: dict[str, str] = {}
    repeat_candidates: list[dict[str, Any]] = []
    lookup_mismatched_result_count = 0

    def pace_api_request() -> None:
        nonlocal api_request_count
        if api_request_count and settings.api_pacing_seconds:
            sleeper(settings.api_pacing_seconds)
        api_request_count += 1

    def candidate_from_apple(item: dict[str, Any], feed_id: str, *, source: str,
                             stratum_key: str, query: str, topic: str,
                             language_context: str, purpose: str,
                             storefront: str, observed_at: str) -> dict[str, Any]:
        genres = item.get("genres", [])
        if not isinstance(genres, list):
            genres = []
        return {
            "record": {
                "feed_id": feed_id,
                "title": str(item.get("collectionName") or ""),
                "website_url": "",
                "description": str(item.get("description") or ""),
                "categories": "; ".join(dict.fromkeys(str(value) for value in genres if value)),
                "language": "",
                "country": "",
                "_feed_url": str(item.get("feedUrl") or ""),
            },
            "provenance": {
                "source": source,
                "query_stratum": stratum_key,
                "query": query,
                "topic_search_context": topic,
                "language_search_context": language_context,
                "sampling_purpose": purpose,
                "storefront": storefront.upper(),
                "observed_at": observed_at,
            },
        }

    valid_repeat_ids = sorted(
        (feed_id for feed_id in prior_ids
         if re.fullmatch(r"itunes:collection:[1-9][0-9]*", feed_id)),
        key=lambda feed_id: int(feed_id.rsplit(":", 1)[1]),
    )
    invalid_prior_id_count = len(prior_ids) - len(valid_repeat_ids)
    requested_repeat_ids = valid_repeat_ids[:repeat_target]
    repeat_storefront = settings.storefront.lower()
    for feed_id in requested_repeat_ids:
        match = re.fullmatch(r"itunes:collection:([1-9][0-9]*)", feed_id)
        if match is None:
            lookup_status_by_feed[feed_id] = "invalid_saved_id"
            continue
        collection_id = match.group(1)
        pace_api_request()
        lookup_api_call_count += 1
        try:
            results = api_lookup(collection_id, replace(settings, storefront=repeat_storefront))
            lookup_result_count += len(results)
        except Exception as exc:
            category = _safe_error_category(exc)
            lookup_status_by_feed[feed_id] = "request_failure"
            lookup_errors.append(f"repeat_lookup_{lookup_api_call_count}: {category}")
            continue
        matching = [item for item in results if _apple_collection_id(item.get("collectionId")) == collection_id]
        lookup_mismatched_result_count += len(results) - len(matching)
        if not matching:
            lookup_status_by_feed[feed_id] = "mismatched_result" if results else "deleted_or_unavailable"
            continue
        item = matching[0]
        if not str(item.get("collectionName") or "").strip():
            lookup_status_by_feed[feed_id] = "missing_required_metadata"
            continue
        if feed_id in seen:
            lookup_status_by_feed[feed_id] = "duplicate"
            continue
        seen.add(feed_id)
        lookup_status_by_feed[feed_id] = "found"
        repeat_candidates.append(candidate_from_apple(
            item, feed_id, source="Apple iTunes Lookup API",
            stratum_key="repeat-id-lookup", query="Apple collection ID lookup",
            topic="not applicable", language_context="not specified",
            purpose="repeat_lookup", storefront=repeat_storefront,
            observed_at=datetime.now(timezone.utc).isoformat(),
        ))

    for term_index, term in enumerate(terms):
        stratum = term if isinstance(term, SearchStratum) else None
        term_key = term_keys[term_index]
        query = stratum.query if stratum else str(term)
        search_settings = replace(settings, storefront=stratum.storefront) if stratum else settings
        if exploration_target <= 0:
            break
        pace_api_request()
        search_call_count += 1
        try:
            results = api_search(query, search_settings)
            api_count += len(results)
            observed_at = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            # Exception text may contain the request URL. Persist only a safe
            # category because the collection report is written to SQLite.
            api_errors.append(f"{term_key}: {_safe_error_category(exc)}")
            continue
        for item in results:
            identifier = _apple_collection_id(item.get("collectionId"))
            title = item.get("collectionName")
            if identifier is None or not title:
                continue
            feed_id = f"itunes:collection:{identifier}"
            if feed_id in seen or feed_id in prior_ids:
                continue
            seen.add(feed_id)
            # Only documented source values enter the sample. The storefront
            # is saved as catalog context and never copied to record country.
            queues[term_key].append(candidate_from_apple(
                item, feed_id, source="Apple iTunes Search API",
                stratum_key=term_key, query=query,
                topic=stratum.topic if stratum else "custom query",
                language_context=stratum.language_context if stratum else "not specified",
                purpose="exploration", storefront=search_settings.storefront,
                observed_at=observed_at,
            ))

    def round_robin_exploration() -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        remaining = {term: list(queues[term]) for term in term_keys}
        while any(remaining.values()):
            for term in term_keys:
                if remaining[term]:
                    output.append(remaining[term].pop(0))
        return output

    exploration_candidates = round_robin_exploration()
    selected_repeats = repeat_candidates[:repeat_target]
    selected_exploration = exploration_candidates[:exploration_target]
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for candidate in (*selected_repeats, *selected_exploration):
        feed_id = candidate["record"]["feed_id"]
        if feed_id in selected_ids:
            continue
        selected.append(candidate)
        selected_ids.add(feed_id)
        if len(selected) >= settings.max_records:
            break
    records = [candidate["record"] for candidate in selected]
    provenance_by_feed = {candidate["record"]["feed_id"]: candidate["provenance"] for candidate in selected}
    actual_repeat_count = sum(record["feed_id"] in prior_ids for record in records)
    actual_new_count = len(records) - actual_repeat_count
    geo_field_sources: dict[str, dict[str, str]] = {
        record["feed_id"]: {
            "title": f"{provenance_by_feed[record['feed_id']]['source']} collectionName",
            "description": f"{provenance_by_feed[record['feed_id']]['source']} description" if record.get("description") else "source not recorded",
        }
        for record in records
    }
    rss_evidence_status: dict[str, str] = {}
    structured_locations_by_feed: dict[str, list[dict[str, str]]] = {}
    location_parse_errors_by_feed: dict[str, int] = {}
    apple_storefront_by_feed = {feed_id: provenance["storefront"] for feed_id, provenance in provenance_by_feed.items()}
    episode_dates_by_feed: dict[str, list[str]] = {}
    rss_observed_at_by_feed: dict[str, str] = {}
    rss_errors: list[str] = []
    episode_samples_by_feed: dict[str, tuple[str, ...]] = {}
    rss_successes = rss_failures = rss_missing = rss_attempted = 0
    rss_timeout_count = 0
    rss_outcome_counts = {
        "dns_failure": 0,
        "connection_tls_failure": 0,
        "http_error": 0,
        "timeout": 0,
        "response_size_limit": 0,
        "xml_parsing_failure": 0,
        "successful_parsing": 0,
        "other_or_unknown_failure": 0,
    }
    rss_started = clock()
    rss_deadline = rss_started + settings.rss_collection_budget_seconds
    rss_timed_out = False

    def mark_remaining_unattempted(start_index: int) -> None:
        nonlocal rss_missing, rss_timeout_count, rss_failures
        for pending in records[start_index:]:
            if pending.get("_feed_url"):
                rss_timeout_count += 1
                rss_failures += 1
                rss_errors.append(f"{pending['feed_id']}: timeout_unattempted")
            else:
                rss_missing += 1
            rss_evidence_status[pending["feed_id"]] = "unavailable"

    for record_index, record in enumerate(records):
        feed_url = record.pop("_feed_url")
        if not feed_url:
            rss_missing += 1
            rss_evidence_status[record["feed_id"]] = "unavailable"
            continue
        remaining = rss_deadline - clock()
        if remaining <= 0:
            rss_timed_out = True
            if feed_url:
                rss_timeout_count += 1
                rss_failures += 1
                rss_errors.append(f"{record['feed_id']}: timeout_unattempted")
            else:
                rss_missing += 1
            rss_evidence_status[record["feed_id"]] = "unavailable"
            mark_remaining_unattempted(record_index + 1)
            break
        if settings.rss_pacing_seconds:
            sleeper(min(settings.rss_pacing_seconds, remaining))
            if clock() >= rss_deadline:
                rss_timed_out = True
                if feed_url:
                    rss_timeout_count += 1
                    rss_failures += 1
                    rss_errors.append(f"{record['feed_id']}: timeout_unattempted")
                else:
                    rss_missing += 1
                rss_evidence_status[record["feed_id"]] = "unavailable"
                mark_remaining_unattempted(record_index + 1)
                break
        try:
            rss_attempted += 1
            if rss_fetcher is fetch_rss_metadata:
                metadata = rss_fetcher(feed_url, settings, deadline_monotonic=min(rss_deadline, clock() + settings.rss_request_timeout_seconds))
            else:
                metadata = rss_fetcher(feed_url, settings)
        except Exception as exc:
            rss_failures += 1
            rss_evidence_status[record["feed_id"]] = "unavailable"
            category = _safe_error_category(exc)
            rss_outcome_counts[_rss_diagnostic_category(category)] += 1
            rss_errors.append(f"{record['feed_id']}: {category}")
            if clock() >= rss_deadline:
                rss_timed_out = True
                mark_remaining_unattempted(record_index + 1)
                break
            continue
        rss_successes += 1
        rss_outcome_counts["successful_parsing"] += 1
        rss_evidence_status[record["feed_id"]] = "checked"
        rss_observed_at_by_feed[record["feed_id"]] = datetime.now(timezone.utc).isoformat()
        for field in ("website_url", "description", "categories", "language"):
            value = getattr(metadata, field)
            if value:
                record[field] = value
                if field in {"title", "description"}:
                    geo_field_sources[record["feed_id"]][field] = "publisher RSS channel metadata"
                elif field == "language":
                    geo_field_sources[record["feed_id"]][field] = "publisher RSS channel language"
        structured_locations_by_feed[record["feed_id"]] = list(getattr(metadata, "locations", ()))
        location_parse_errors_by_feed[record["feed_id"]] = int(getattr(metadata, "location_parse_errors", 0) or 0)
        samples = getattr(metadata, "episode_text_samples", ())
        if samples:
            episode_samples_by_feed[record["feed_id"]] = tuple(samples[:8])
        episode_dates = getattr(metadata, "episode_publication_dates", ())
        if episode_dates:
            valid_dates = []
            for value in episode_dates[:settings.max_episode_dates_per_feed]:
                try:
                    parsed_date = date.fromisoformat(str(value))
                except ValueError:
                    continue
                valid_dates.append(parsed_date.isoformat())
            if valid_dates:
                episode_dates_by_feed[record["feed_id"]] = valid_dates
    missing = {field: sum(not str(row.get(field, "")).strip() for row in records) for field in CSV_FIELDS}
    today = date.today()
    newest_episode_age_days = {
        feed_id: max(0, (today - max(date.fromisoformat(value) for value in values)).days)
        for feed_id, values in episode_dates_by_feed.items() if values
    }
    rss_outcome_counts["missing_feed_url"] = rss_missing
    rss_attempt_outcome_total = sum(
        rss_outcome_counts[name] for name in (
            "dns_failure", "connection_tls_failure", "http_error", "timeout",
            "response_size_limit", "xml_parsing_failure", "successful_parsing",
            "other_or_unknown_failure",
        )
    )
    rss_record_outcome_total = rss_attempted + rss_missing + rss_timeout_count
    lookup_outcome_counts = {
        outcome: sum(status == outcome for status in lookup_status_by_feed.values())
        for outcome in (
            "found", "deleted_or_unavailable", "mismatched_result",
            "request_failure", "missing_required_metadata", "duplicate",
        )
    }
    report = {
        "collection_date": today.isoformat(),
        "source": "Apple iTunes Lookup API, Apple iTunes Search API, and publisher RSS channel metadata",
        "api_endpoint": ITUNES_SEARCH_URL,
        "storefront": "Multiple Apple search storefronts (sampling context only; not publisher country)",
        "search_terms": [term.query if isinstance(term, SearchStratum) else str(term) for term in terms],
        "search_strata": [
            {"label": term.label, "topic_search_context": term.topic,
             "language_search_context": term.language_context, "storefront": term.storefront.upper()}
            for term in terms if isinstance(term, SearchStratum)
        ],
        "sampling": {
            "method": "deterministic stable-ID lookup for repeats plus round-robin exploration across search strata",
            "max_records": settings.max_records,
            "repeat_target": repeat_target,
            "exploration_target": exploration_target,
            "repeat_ids_selected_for_lookup": len(requested_repeat_ids),
            "previously_audited_ids_unsupported": invalid_prior_id_count,
            "actual_repeat_count": actual_repeat_count,
            "actual_new_count": actual_new_count,
            "repeat_shortfall": max(0, repeat_target - actual_repeat_count),
            "exploration_shortfall": max(0, exploration_target - actual_new_count),
            "previously_audited_ids_available": len(prior_ids),
            "repeat_lookup_outcomes": lookup_outcome_counts,
            "repeat_lookup_outcome_total": sum(lookup_outcome_counts.values()),
            "repeat_lookup_outcomes_reconcile": sum(lookup_outcome_counts.values()) == len(requested_repeat_ids),
            "repeat_lookup_no_matches": bool(requested_repeat_ids) and actual_repeat_count == 0 and not lookup_errors,
            "record_cap_shortfall": max(0, settings.max_records - len(records)),
        },
        "api_result_instances": api_count + lookup_result_count,
        "unique_candidates": len(seen),
        "unique_records_retained": len(records),
        "rss_retrieval_successes": rss_successes,
        "rss_retrieval_failures": rss_failures,
        "rss_collection_timed_out": rss_timed_out,
        "rss_timeout_unattempted_feeds": rss_timeout_count,
        "rss_failure_feeds_unattempted_due_to_deadline": rss_timeout_count,
        "rss_collection_elapsed_seconds": round(max(0.0, clock() - rss_started), 3),
        "rss_collection_budget_seconds": settings.rss_collection_budget_seconds,
        "rss_request_timeout_seconds": settings.rss_request_timeout_seconds,
        "missing_rss_feed_urls": rss_missing,
        "rss_attempted_count": rss_attempted,
        "rss_outcome_counts": rss_outcome_counts,
        "rss_attempt_outcome_total": rss_attempt_outcome_total,
        "rss_attempt_counts_reconcile": rss_attempt_outcome_total == rss_attempted,
        "rss_record_outcome_total": rss_record_outcome_total,
        "rss_record_counts_reconcile": rss_record_outcome_total == len(records),
        "missing_field_counts": missing,
        "api_search_errors": api_errors,
        "api_lookup_errors": lookup_errors,
        "api_lookup_mismatched_result_count": lookup_mismatched_result_count,
        "rss_errors": rss_errors,
        "record_provenance_by_feed": provenance_by_feed,
        "rss_observed_at_by_feed": rss_observed_at_by_feed,
        "episode_publication_dates_by_feed": episode_dates_by_feed,
        "episode_date_evidence": {
            "feeds_with_valid_dates": len(episode_dates_by_feed),
            "valid_publication_dates": sum(map(len, episode_dates_by_feed.values())),
            "newest_observed_episode_age_days_by_feed": newest_episode_age_days,
            "maximum_dates_per_feed": settings.max_episode_dates_per_feed,
            "listener_counts_available": False,
        },
        "geo_field_sources": geo_field_sources,
        "rss_evidence_status_by_feed": rss_evidence_status,
        "structured_locations_by_feed": structured_locations_by_feed,
        "location_parse_errors_by_feed": location_parse_errors_by_feed,
        "apple_storefront_by_feed": apple_storefront_by_feed,
        "episode_text_samples_by_feed": episode_samples_by_feed,
        "limitations": [
            "Repeat records are requested by saved stable Apple collection ID; unavailable IDs remain shortfalls and are not replaced.",
            "Search coverage depends on configured Apple topic, language-query, and storefront strata; it is not a complete directory or globally representative sample.",
            "Topic, language-query, storefront, and chart context are sampling metadata only; publisher country and podcast origin are not inferred.",
            "Only API-provided feedUrl values are fetched. RSS website links supply publisher website metadata; feed URLs are never substituted as websites.",
            "RSS requests are sequential, use strict verified TLS, are DNS-pinned to validated public addresses, response-size limited, timeout bounded, and redirects are manually revalidated.",
            "Episode publication dates are publisher-provided RSS evidence; they do not establish audience size or listening activity.",
            "Descriptions and arbitrary RSS links are text only and are never fetched.",
        ],
    }
    report["api_search_calls"] = search_call_count
    report["api_lookup_calls"] = lookup_api_call_count
    report["api_request_count"] = api_request_count
    report["search_terms"] = list(term_keys)
    report["search_queries"] = [term.query if isinstance(term, SearchStratum) else str(term) for term in terms]
    report["search_strata"] = [
        {"label": term.label, "topic_search_context": term.topic,
         "language_search_context": term.language_context,
         "sampling_purpose": term.sampling_purpose, "storefront": term.storefront.upper()}
        for term in terms if isinstance(term, SearchStratum)
    ]
    report["sampling"]["exploration_overflow_count"] = max(0, actual_new_count - exploration_target)
    report["sampling"]["record_cap_shortfall"] = max(0, settings.max_records - len(records))
    return CollectionResult(tuple({field: row.get(field, "") for field in CSV_FIELDS} for row in records), report)

def render_report(report: dict[str, Any]) -> str:
    sampling = report.get("sampling", {})
    episode = report.get("episode_date_evidence", {})
    queries = report.get("search_queries", report["search_terms"])
    rss_counts = report.get("rss_outcome_counts", {})
    lines = [
        "# Real Podcast Dataset Collection Report", "",
        f"- Collection date: {report['collection_date']}",
        f"- Source: {report['source']}",
        f"- API endpoint: `{report['api_endpoint']}`",
        f"- Search catalog: {report['storefront']}",
        f"- Search strata: {len(report['search_terms'])}",
        f"- Search queries: {', '.join(queries)}",
        f"- API requests: {report.get('api_lookup_calls', 0)} ID lookups, {report.get('api_search_calls', 0)} searches",
        f"- API result instances received: {report['api_result_instances']}",
        f"- Unique records retained: {report['unique_records_retained']}",
        f"- Sampling: {sampling.get('actual_repeat_count', 0)} repeated, {sampling.get('actual_new_count', 0)} new; repeat shortfall {sampling.get('repeat_shortfall', 0)}, exploration shortfall {sampling.get('exploration_shortfall', 0)}, record-cap shortfall {sampling.get('record_cap_shortfall', 0)}",
        f"- RSS retrieval: {report['rss_retrieval_successes']} parsed, {report['rss_retrieval_failures']} failed or unattempted, {report['missing_rss_feed_urls']} missing feed URLs",
        f"- RSS attempts: {report.get('rss_attempted_count', 0)}; outcome total: {report.get('rss_attempt_outcome_total', 0)}; reconcile: {report.get('rss_attempt_counts_reconcile', False)}",
        f"- RSS record coverage reconciliation: {report.get('rss_record_outcome_total', 0)} / {report.get('unique_records_retained', 0)}; reconcile: {report.get('rss_record_counts_reconcile', False)}",
        "- RSS outcomes: " + ", ".join(f"{name}={rss_counts.get(name, 0)}" for name in (
            "missing_feed_url", "dns_failure", "connection_tls_failure", "http_error",
            "timeout", "response_size_limit", "xml_parsing_failure",
            "successful_parsing", "other_or_unknown_failure",
        )),
        f"- RSS stage: {report.get('rss_collection_elapsed_seconds', 0)} seconds of {report.get('rss_collection_budget_seconds', 0)}; deadline reached: {report.get('rss_collection_timed_out', False)}; feed URLs unattempted at deadline: {report.get('rss_timeout_unattempted_feeds', 0)}",
        f"- Episode publication-date evidence: {episode.get('feeds_with_valid_dates', 0)} feeds, {episode.get('valid_publication_dates', 0)} valid dates",
        "", "## Missing field counts", "",
        *[f"- `{name}`: {count}" for name, count in report["missing_field_counts"].items()],
        "", "## Provenance and limitations", "",
        *[f"- {item}" for item in report["limitations"]],
    ]
    if report["api_lookup_errors"]:
        lines.extend(["", "## API lookup errors", "", *[f"- {error}" for error in report["api_lookup_errors"]]])
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
    parser.add_argument("--repeat-target", type=int, help="Target prior records (default up to 35).")
    parser.add_argument("--exploration-target", type=int, help="Target new records (default up to 15).")
    parser.add_argument("--rss-collection-budget-seconds", type=float, default=240.0)
    parser.add_argument("--output", default="data/real_podcasts.csv")
    parser.add_argument("--report", default="reports/real_podcasts_collection.md")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    output, report = Path(args.output), Path(args.report)
    if output.exists() or report.exists():
        parser.error("Output or report already exists; choose new paths. Existing files are never overwritten.")
    try:
        result = collect_podcasts(CollectorConfig(max_records=args.limit, repeat_target=args.repeat_target, exploration_target=args.exploration_target, rss_collection_budget_seconds=args.rss_collection_budget_seconds))
        write_collection(result, output, report)
    except (CollectionError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"dataset": str(output.resolve()), "report": str(report.resolve()), **result.report}, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())

