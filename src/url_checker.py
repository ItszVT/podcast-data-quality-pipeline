"""Conservative, evidence-backed checks for public website URLs.

DNS is resolved with bounded dnspython queries, every answer is checked, and
libcurl is pinned to one approved address for each request while it retains the
original URL hostname for Host and TLS verification. Redirects are manual.
"""

from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import dns.exception
import dns.resolver
from curl_cffi import CurlOpt
from curl_cffi.requests import Session as CurlSession
from curl_cffi.requests.exceptions import (
    CertificateVerifyError,
    DNSError,
    RequestException,
    SSLError,
    Timeout as CurlTimeout,
)
from bs4 import BeautifulSoup

LIKELY_WORKING = "likely_working"
HTTP_ERROR = "http_error"
SUSPECTED_SOFT_404 = "suspected_soft_404"
INCONCLUSIVE = "inconclusive"
INVALID_INPUT = "invalid_input"

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SOFT_404_PATTERNS = (
    re.compile(r"\bpage not found\b", re.IGNORECASE),
    re.compile(r"\b404 not found\b", re.IGNORECASE),
    re.compile(r"\bpage (?:does not|doesn't) exist\b", re.IGNORECASE),
    re.compile(r"\bpage you (?:are )?looking for (?:was |could be )?not found\b", re.IGNORECASE),
    re.compile(r"\b(?:could not|couldn't) find (?:the )?page\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class UrlCheckerConfig:
    """Limits applied to request attempts and one sequential URL check."""

    connect_timeout_seconds: float = 3.0
    request_timeout_seconds: float = 5.0
    dns_timeout_seconds: float = 2.0
    stage_budget_seconds: float = 90.0
    max_response_bytes: int = 1_000_000
    max_redirects: int = 5

    def __post_init__(self) -> None:
        if min(
            self.connect_timeout_seconds, self.request_timeout_seconds,
            self.dns_timeout_seconds, self.stage_budget_seconds,
        ) <= 0:
            raise ValueError("Timeouts and checking budgets must be greater than zero.")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be greater than zero.")
        if self.max_redirects < 0:
            raise ValueError("max_redirects cannot be negative.")
        if self.connect_timeout_seconds > 5 or self.request_timeout_seconds > 5 or self.dns_timeout_seconds > 5:
            raise ValueError("Request timeouts cannot exceed five seconds.")
        if self.stage_budget_seconds > 90:
            raise ValueError("The website-checking stage cannot exceed 90 seconds.")
        if self.max_redirects > 5 or self.max_response_bytes > 1_000_000:
            raise ValueError("Redirect and response limits exceed the supported safety bounds.")


@dataclass(frozen=True)
class RedirectHop:
    source_url: str
    status_code: int
    destination_url: str


@dataclass(frozen=True)
class UrlCheckResult:
    original_url: str
    final_url: str | None
    http_status_code: int | None
    redirect_count: int
    redirect_chain: tuple[RedirectHop, ...]
    response_time_ms: float | None
    classification: str
    evidence: str
    error_reason: str | None = None
    page_title: str | None = None


def _resolve_host(host: str, port: int, timeout_seconds: float) -> list[str]:
    """Resolve A and AAAA records with one shared, finite DNS lifetime."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass

    absolute_name = host.encode("idna").decode("ascii").rstrip(".") + "."
    deadline = time.monotonic() + timeout_seconds
    resolver = dns.resolver.Resolver(configure=True)
    addresses: list[str] = []
    for record_type in ("A", "AAAA"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise dns.exception.Timeout
        try:
            answer = resolver.resolve(
                absolute_name,
                record_type,
                lifetime=remaining,
                search=False,
                raise_on_no_answer=False,
            )
        except dns.resolver.NoAnswer:
            continue
        addresses.extend(record.address for record in answer)
    return list(dict.fromkeys(addresses))


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _validated_destination(
    url: str, *, deadline_monotonic: float, dns_timeout_seconds: float = 2.0
) -> tuple[str | None, str | None, str | None, str | None, str | None, int | None]:
    """Return URL, fixed failure category, host, approved IP, and port."""
    candidate = url.strip()
    if not candidate:
        return None, None, "blank_url", None, None, None
    if any(character.isspace() for character in candidate):
        return candidate, None, "malformed_url", None, None, None
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return candidate, None, "malformed_url", None, None, None
    if port == 0:
        return candidate, None, "malformed_url", None, None, None

    scheme = parts.scheme.lower()
    if not parts.netloc or not parts.hostname:
        return candidate, None, "malformed_url", None, None, None
    if scheme not in {"http", "https"}:
        return candidate, None, "unsupported_scheme", None, None, None
    if parts.username is not None or parts.password is not None:
        return candidate, "url_credentials_not_allowed", None, None, None, None

    try:
        curl_hostname = parts.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return candidate, None, "malformed_url", None, None, None
    host = curl_hostname.rstrip(".")
    if not host:
        return candidate, None, "malformed_url", None, None, None
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return candidate, "prohibited_hostname", None, None, None, None
    if host in {
        "metadata", "metadata.google.internal", "metadata.azure.internal", "instance-data"
    }:
        return candidate, "prohibited_hostname", None, None, None, None
    port = port or (443 if scheme == "https" else 80)

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return candidate, "check_budget_exhausted", None, None, None, None
    try:
        addresses = _resolve_host(host, port, min(remaining, dns_timeout_seconds))
    except (dns.exception.DNSException, OSError, ValueError):
        return candidate, "dns_resolution_failed", None, None, None, None
    if not addresses:
        return candidate, "dns_resolution_failed", None, None, None, None
    if not all(_is_public_address(address) for address in addresses):
        return candidate, "prohibited_ip_destination", None, None, None, None

    # Only one approved address is installed in this transfer's fresh libcurl
    # handle. It cannot resolve again or fail over to an unchecked address.
    return candidate, None, None, curl_hostname, addresses[0], port


def _validate_destination(url: str) -> tuple[str | None, str | None, str | None]:
    """Return (trimmed URL, safety failure, malformed/unsupported input error)."""
    candidate, safety_error, input_error, _, _, _ = _validated_destination(
        url, deadline_monotonic=time.monotonic() + 5.0
    )
    return candidate, safety_error, input_error


def _soft_404_signals(body: bytes, content_type: str) -> tuple[str | None, tuple[str, ...]]:
    if "html" not in content_type.lower() and "text/" not in content_type.lower():
        return None, ()
    soup = BeautifulSoup(body, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    heading = soup.find(["h1", "h2"])
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    visible_text = soup.get_text(" ", strip=True)

    signals: list[str] = []
    for label, text in (("title", title or ""), ("heading", heading_text)):
        match = next((pattern.search(text) for pattern in _SOFT_404_PATTERNS if pattern.search(text)), None)
        if match:
            signals.append(f"{label}: {match.group(0)}")
    # Body-only matches are weaker: avoid flagging a long article that merely
    # discusses an error phrase. A short page made up of the signal is stronger.
    if not signals and len(visible_text) <= 300:
        match = next((pattern.search(visible_text) for pattern in _SOFT_404_PATTERNS if pattern.search(visible_text)), None)
        if match:
            signals.append(f"short page text: {match.group(0)}")
    return title, tuple(signals)


def _result(
    original_url: str,
    started_at: float,
    classification: str,
    evidence: str,
    *,
    final_url: str | None = None,
    status: int | None = None,
    redirects: list[RedirectHop] | None = None,
    error_reason: str | None = None,
    page_title: str | None = None,
) -> UrlCheckResult:
    return UrlCheckResult(
        original_url=original_url,
        final_url=final_url,
        http_status_code=status,
        redirect_count=len(redirects or []),
        redirect_chain=tuple(redirects or []),
        response_time_ms=round((time.perf_counter() - started_at) * 1000, 2),
        classification=classification,
        evidence=evidence,
        error_reason=error_reason,
        page_title=page_title,
    )


def check_website_url(
    url: object,
    config: UrlCheckerConfig | None = None,
    *,
    deadline_monotonic: float | None = None,
) -> UrlCheckResult:
    """Check one HTTP(S) URL with manual, safety-checked redirects.

    No requests are made for invalid inputs or prohibited destinations. DNS
    checks reject the host if any resolved address is non-global. Environment
    proxies are disabled so an ambient proxy cannot change the destination.
    """
    settings = config or UrlCheckerConfig()
    original_url = "" if url is None else str(url)
    started_at = time.perf_counter()
    stage_deadline = deadline_monotonic or (time.monotonic() + settings.stage_budget_seconds)
    redirects: list[RedirectHop] = []
    current_url = original_url.strip()
    visited = {current_url}
    last_status: int | None = None
    body = bytearray()
    body_too_large = False
    while True:
        if time.monotonic() >= stage_deadline:
            return _result(original_url, started_at, INCONCLUSIVE, "The website-checking time budget was exhausted.", final_url=current_url, status=last_status, redirects=redirects, error_reason="check_budget_exhausted")
        attempt_deadline = min(stage_deadline, time.monotonic() + 5.0)
        candidate, safety_error, input_error, host, approved_ip, port = _validated_destination(current_url, deadline_monotonic=attempt_deadline, dns_timeout_seconds=settings.dns_timeout_seconds)
        if input_error:
            return _result(original_url, started_at, INVALID_INPUT, f"Input URL is invalid: {input_error.replace('_', ' ')}.", error_reason=input_error)
        if safety_error or candidate is None:
            reason = safety_error or "malformed_url"
            return _result(original_url, started_at, INCONCLUSIVE, f"Request was blocked by URL safety checks: {reason.replace('_', ' ')}.", final_url=candidate, status=last_status, redirects=redirects, error_reason=reason)
        assert host and approved_ip and port
        attempt_timeout = min(settings.request_timeout_seconds, 5.0, attempt_deadline - time.monotonic())
        if attempt_timeout <= 0:
            return _result(original_url, started_at, INCONCLUSIVE, "The website-checking time budget was exhausted.", final_url=candidate, status=last_status, redirects=redirects, error_reason="check_budget_exhausted")
        body.clear()
        body_too_large = False

        def collect_chunk(chunk: bytes) -> int:
            nonlocal body_too_large
            if len(body) + len(chunk) > settings.max_response_bytes:
                body_too_large = True
                return 0
            body.extend(chunk)
            return len(chunk)

        curl_host = f"[{host}]" if ":" in host else host
        curl_ip = f"[{approved_ip}]" if ":" in approved_ip else approved_ip
        curl_options = {
            CurlOpt.PROTOCOLS_STR: "http,https",
            CurlOpt.SSL_VERIFYPEER: 1,
            CurlOpt.SSL_VERIFYHOST: 2,
            CurlOpt.TIMEOUT_MS: max(1, int(attempt_timeout * 1000)),
            CurlOpt.CONNECTTIMEOUT_MS: max(1, int(min(settings.connect_timeout_seconds, attempt_timeout) * 1000)),
            CurlOpt.NOSIGNAL: 1,
        }
        try:
            ipaddress.ip_address(host)
            is_ip_literal = True
        except ValueError:
            is_ip_literal = False
        if not is_ip_literal:
            curl_options[CurlOpt.RESOLVE] = [f"{curl_host}:{port}:{curl_ip}"]
        try:
            with CurlSession(
                trust_env=False,
                retry=0,
                timeout=attempt_timeout,
                allow_redirects=False,
                max_redirects=0,
                verify=True,
                proxies={"http": "", "https": ""},
                curl_options=curl_options,
            ) as session:
                response = session.get(
                    candidate,
                    headers={"User-Agent": "PodcastDataQualityChecker/0.1", "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1", "Accept-Encoding": "identity"},
                    allow_redirects=False,
                    max_redirects=0,
                    verify=True,
                    proxies={"http": "", "https": ""},
                    content_callback=collect_chunk,
                )
        except CurlTimeout:
            return _result(original_url, started_at, INCONCLUSIVE, "The request timed out.", final_url=candidate, status=last_status, redirects=redirects, error_reason="timeout")
        except CertificateVerifyError:
            return _result(original_url, started_at, INCONCLUSIVE, "TLS certificate verification failed.", final_url=candidate, status=last_status, redirects=redirects, error_reason="tls_verification_failed")
        except SSLError:
            return _result(original_url, started_at, INCONCLUSIVE, "The secure connection could not be established.", final_url=candidate, status=last_status, redirects=redirects, error_reason="tls_connection_error")
        except DNSError:
            return _result(original_url, started_at, INCONCLUSIVE, "The approved destination could not be reached.", final_url=candidate, status=last_status, redirects=redirects, error_reason="dns_resolution_failed")
        except RequestException:
            # Never persist the library exception; it may contain a hostname or URL.
            if body_too_large:
                return _result(original_url, started_at, INCONCLUSIVE, f"Response exceeded the {settings.max_response_bytes}-byte limit.", final_url=candidate, status=last_status, redirects=redirects, error_reason="response_too_large")
            return _result(original_url, started_at, INCONCLUSIVE, "The request could not be completed.", final_url=candidate, status=last_status, redirects=redirects, error_reason="connection_error")
        except Exception:
            return _result(original_url, started_at, INCONCLUSIVE, "The request could not be completed.", final_url=candidate, status=last_status, redirects=redirects, error_reason="connection_error")

        last_status = response.status_code
        if body_too_large:
            return _result(original_url, started_at, INCONCLUSIVE, f"Response exceeded the {settings.max_response_bytes}-byte limit.", final_url=candidate, status=last_status, redirects=redirects, error_reason="response_too_large")
        location = response.headers.get("Location")
        if response.status_code in _REDIRECT_STATUSES and location:
            destination = urljoin(candidate, location)
            redirects.append(RedirectHop(candidate, response.status_code, destination))
            if destination in visited:
                return _result(original_url, started_at, INCONCLUSIVE, "A redirect loop was detected.", final_url=candidate, status=last_status, redirects=redirects, error_reason="redirect_loop")
            if len(redirects) > settings.max_redirects:
                return _result(original_url, started_at, INCONCLUSIVE, "The configured redirect limit was exceeded.", final_url=candidate, status=last_status, redirects=redirects, error_reason="max_redirects_exceeded")
            # Validate now for prompt refusal, then again immediately before
            # the destination request to prevent DNS changes between hops.
            redirect_validation_deadline = min(stage_deadline, time.monotonic() + 5.0)
            _, redirect_safety, redirect_input, _, _, _ = _validated_destination(destination, deadline_monotonic=redirect_validation_deadline, dns_timeout_seconds=settings.dns_timeout_seconds)
            if redirect_safety or redirect_input:
                reason = redirect_safety or redirect_input
                return _result(original_url, started_at, INCONCLUSIVE, f"Redirect destination was blocked by URL safety checks: {reason.replace('_', ' ')}.", final_url=candidate, status=last_status, redirects=redirects, error_reason=reason)
            visited.add(destination)
            current_url = destination
            continue
        if 300 <= response.status_code < 400:
            return _result(original_url, started_at, INCONCLUSIVE, "The response was a redirect without a usable Location header.", final_url=candidate, status=last_status, redirects=redirects, error_reason="redirect_without_location")
        if response.status_code >= 400:
            return _result(original_url, started_at, HTTP_ERROR, f"Final response returned HTTP {response.status_code}.", final_url=candidate, status=last_status, redirects=redirects)
        content_type = response.headers.get("Content-Type", "")
        page_title, signals = _soft_404_signals(bytes(body), content_type)
        if 200 <= response.status_code < 300 and signals:
            evidence = "Potential error-page signals detected: " + "; ".join(signals[:2]) + "."
            return _result(original_url, started_at, SUSPECTED_SOFT_404, evidence, final_url=candidate, status=last_status, redirects=redirects, page_title=page_title)
        if 200 <= response.status_code < 300:
            return _result(original_url, started_at, LIKELY_WORKING, f"Final response returned HTTP {response.status_code}; no configured soft-404 signals were detected.", final_url=candidate, status=last_status, redirects=redirects, page_title=page_title)
        return _result(original_url, started_at, INCONCLUSIVE, f"Final response returned unexpected HTTP status {response.status_code}.", final_url=candidate, status=last_status, redirects=redirects, error_reason="unexpected_http_status")


