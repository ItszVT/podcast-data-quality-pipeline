"""Conservative, evidence-backed checks for public website URLs.

Redirects are handled manually so every destination receives the same URL and
DNS safety checks. Requests is used for HTTP transport; because it performs a
new DNS lookup when connecting, DNS rebinding remains a documented limitation.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests
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
    """Limits applied to one sequential URL check."""

    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 5.0
    max_response_bytes: int = 1_000_000
    max_redirects: int = 5

    def __post_init__(self) -> None:
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("Connection and read timeouts must be greater than zero.")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be greater than zero.")
        if self.max_redirects < 0:
            raise ValueError("max_redirects cannot be negative.")


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


def _resolve_host(host: str, port: int) -> list[str]:
    """Resolve a hostname; IP literals are returned without a DNS lookup."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise OSError(f"DNS resolution failed: {exc}") from exc
        return list(dict.fromkeys(answer[4][0].split("%", 1)[0] for answer in answers))


def _validate_destination(url: str) -> tuple[str | None, str | None, str | None]:
    """Return (trimmed URL, safety failure, malformed/unsupported input error)."""
    candidate = url.strip()
    if not candidate:
        return None, None, "blank_url"
    if any(character.isspace() for character in candidate):
        return candidate, None, "malformed_url"

    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return candidate, None, "malformed_url"

    scheme = parts.scheme.lower()
    if not parts.netloc or not parts.hostname:
        return candidate, None, "malformed_url"
    if scheme not in {"http", "https"}:
        return candidate, None, "unsupported_scheme"
    if parts.username is not None or parts.password is not None:
        return candidate, "url_credentials_not_allowed", None

    host = parts.hostname.rstrip(".").lower()
    if not host:
        return candidate, None, "malformed_url"
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return candidate, "prohibited_hostname", None
    port = port or (443 if scheme == "https" else 80)

    try:
        addresses = _resolve_host(host, port)
    except OSError:
        return candidate, "dns_resolution_failed", None
    if not addresses:
        return candidate, "dns_resolution_failed", None

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return candidate, "dns_resolution_failed", None
        if not ip.is_global:
            return candidate, "prohibited_ip_destination", None

    return candidate, None, None


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


def check_website_url(url: object, config: UrlCheckerConfig | None = None) -> UrlCheckResult:
    """Check one HTTP(S) URL with manual, safety-checked redirects.

    No requests are made for invalid inputs or prohibited destinations. DNS
    checks reject the host if any resolved address is non-global. Environment
    proxies are disabled so an ambient proxy cannot change the destination.
    """
    settings = config or UrlCheckerConfig()
    original_url = "" if url is None else str(url)
    started_at = time.perf_counter()
    redirects: list[RedirectHop] = []
    candidate, safety_error, input_error = _validate_destination(original_url)
    if input_error:
        return _result(
            original_url, started_at, INVALID_INPUT,
            f"Input URL is invalid: {input_error.replace('_', ' ')}.",
            error_reason=input_error,
        )
    if safety_error:
        return _result(
            original_url, started_at, INCONCLUSIVE,
            f"Request was blocked by URL safety checks: {safety_error.replace('_', ' ')}.",
            final_url=candidate, error_reason=safety_error,
        )

    assert candidate is not None
    session = requests.Session()
    session.trust_env = False
    current_url = candidate
    visited = {current_url}
    last_status: int | None = None

    try:
        while True:
            # Check immediately before each request. Requests performs its own
            # DNS lookup at connect time, so a rebinding race remains possible.
            current_url, safety_error, input_error = _validate_destination(current_url)
            if input_error or safety_error or current_url is None:
                reason = safety_error or input_error or "malformed_url"
                return _result(
                    original_url, started_at, INCONCLUSIVE,
                    f"Redirect destination was blocked by URL safety checks: {reason.replace('_', ' ')}.",
                    final_url=redirects[-1].source_url if redirects else None,
                    status=last_status, redirects=redirects, error_reason=reason,
                )

            try:
                response = session.get(
                    current_url,
                    headers={
                        "User-Agent": "PodcastDataQualityChecker/0.1 (+local portfolio project)",
                        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                        "Accept-Encoding": "identity",
                    },
                    timeout=(settings.connect_timeout_seconds, settings.read_timeout_seconds),
                    allow_redirects=False,
                    stream=True,
                )
            except requests.Timeout:
                return _result(
                    original_url, started_at, INCONCLUSIVE, "The request timed out.",
                    final_url=current_url, status=last_status, redirects=redirects,
                    error_reason="timeout",
                )
            except requests.RequestException as exc:
                return _result(
                    original_url, started_at, INCONCLUSIVE,
                    f"The request could not be completed ({type(exc).__name__}).",
                    final_url=current_url, status=last_status, redirects=redirects,
                    error_reason="connection_error",
                )

            with response:
                last_status = response.status_code
                location = response.headers.get("Location")
                if response.status_code in _REDIRECT_STATUSES and location:
                    destination = urljoin(current_url, location)
                    redirects.append(RedirectHop(current_url, response.status_code, destination))
                    if destination in visited:
                        return _result(
                            original_url, started_at, INCONCLUSIVE, "A redirect loop was detected.",
                            final_url=current_url, status=last_status, redirects=redirects,
                            error_reason="redirect_loop",
                        )
                    if len(redirects) > settings.max_redirects:
                        return _result(
                            original_url, started_at, INCONCLUSIVE,
                            "The configured redirect limit was exceeded.",
                            final_url=current_url, status=last_status, redirects=redirects,
                            error_reason="max_redirects_exceeded",
                        )
                    _, destination_safety_error, destination_input_error = _validate_destination(destination)
                    if destination_safety_error or destination_input_error:
                        reason = destination_safety_error or destination_input_error
                        return _result(
                            original_url, started_at, INCONCLUSIVE,
                            f"Redirect destination was blocked by URL safety checks: {reason.replace('_', ' ')}.",
                            final_url=current_url, status=last_status, redirects=redirects,
                            error_reason=reason,
                        )
                    visited.add(destination)
                    current_url = destination
                    continue

                if 300 <= response.status_code < 400:
                    return _result(
                        original_url, started_at, INCONCLUSIVE,
                        "The response was a redirect without a usable Location header.",
                        final_url=current_url, status=last_status, redirects=redirects,
                        error_reason="redirect_without_location",
                    )
                if response.status_code >= 400:
                    return _result(
                        original_url, started_at, HTTP_ERROR,
                        f"Final response returned HTTP {response.status_code}.",
                        final_url=current_url, status=last_status, redirects=redirects,
                    )

                try:
                    content_length = response.headers.get("Content-Length")
                    try:
                        declared_length = int(content_length) if content_length else None
                    except ValueError:
                        declared_length = None
                    if declared_length is not None and declared_length > settings.max_response_bytes:
                        return _result(
                            original_url, started_at, INCONCLUSIVE,
                            f"Response exceeded the {settings.max_response_bytes}-byte limit.",
                            final_url=current_url, status=last_status, redirects=redirects,
                            error_reason="response_too_large",
                        )
                    chunks: list[bytes] = []
                    received = 0
                    chunk_size = min(16_384, settings.max_response_bytes + 1)
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > settings.max_response_bytes:
                            return _result(
                                original_url, started_at, INCONCLUSIVE,
                                f"Response exceeded the {settings.max_response_bytes}-byte limit.",
                                final_url=current_url, status=last_status, redirects=redirects,
                                error_reason="response_too_large",
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                except requests.Timeout:
                    return _result(
                        original_url, started_at, INCONCLUSIVE,
                        "The response body timed out while being read.",
                        final_url=current_url, status=last_status, redirects=redirects,
                        error_reason="timeout",
                    )
                except requests.RequestException as exc:
                    return _result(
                        original_url, started_at, INCONCLUSIVE,
                        f"The response body could not be read ({type(exc).__name__}).",
                        final_url=current_url, status=last_status, redirects=redirects,
                        error_reason="connection_error",
                    )

                page_title, signals = _soft_404_signals(body, response.headers.get("Content-Type", ""))
                if 200 <= response.status_code < 300 and signals:
                    evidence = "Potential error-page signals detected: " + "; ".join(signals[:2]) + "."
                    return _result(
                        original_url, started_at, SUSPECTED_SOFT_404, evidence,
                        final_url=current_url, status=last_status, redirects=redirects,
                        page_title=page_title,
                    )
                if 200 <= response.status_code < 300:
                    evidence = f"Final response returned HTTP {response.status_code}; no configured soft-404 signals were detected."
                    return _result(
                        original_url, started_at, LIKELY_WORKING, evidence,
                        final_url=current_url, status=last_status, redirects=redirects,
                        page_title=page_title,
                    )
                return _result(
                    original_url, started_at, INCONCLUSIVE,
                    f"Final response returned unexpected HTTP status {response.status_code}.",
                    final_url=current_url, status=last_status, redirects=redirects,
                    error_reason="unexpected_http_status",
                )
    finally:
        session.close()


