from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src import url_checker
from src.url_checker import (
    HTTP_ERROR,
    INCONCLUSIVE,
    INVALID_INPUT,
    LIKELY_WORKING,
    SUSPECTED_SOFT_404,
    UrlCheckerConfig,
    check_website_url,
)


class ControlledHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            self._respond(200, "<html><title>Sample show</title><h1>Welcome</h1></html>")
        elif self.path == "/404":
            self._respond(404, "<html><title>Missing</title><h1>Not here</h1></html>")
        elif self.path == "/soft404":
            self._respond(200, "<html><title>Page Not Found</title><h1>Page Not Found</h1></html>")
        elif self.path == "/article-mentions-error":
            article = "This long article discusses the phrase page not found as an example. " * 12
            self._respond(200, f"<html><title>Language Notes</title><article>{article}</article></html>")
        elif self.path == "/redirect-working":
            self._redirect("/ok")
        elif self.path == "/redirect-error":
            self._redirect("/404")
        elif self.path == "/redirect-private":
            self._redirect(f"http://127.0.0.2:{self.server.server_port}/private")
        elif self.path == "/private":
            self._respond(200, "<html><title>Should not be requested</title></html>")
        elif self.path == "/slow":
            time.sleep(0.25)
            self._respond(200, "<html><title>Slow</title></html>")
        elif self.path == "/slow-stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                for _ in range(10):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.04)
            except OSError:
                pass
        elif self.path == "/large":
            self._respond(200, "x" * 2048)
        elif self.path == "/large-streamed":
            body = b"x" * 2048
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass
        else:
            self._respond(200, "ok")

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _respond(self, status, body):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass

    def log_message(self, *_args):
        pass


@pytest.fixture
def local_server(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ControlledHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Test-only seam approves the fixture's loopback address. The transport
    # connects to exactly this resolved address; production has no bypass.
    monkeypatch.setattr(url_checker, "_resolve_host", lambda host, port, timeout: [host] if host.startswith("127.") else ["127.0.0.1"] if host == "podcast.test" else [])
    monkeypatch.setattr(url_checker, "_is_public_address", lambda address: address == "127.0.0.1")
    yield f"http://podcast.test:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_normal_200_is_likely_working(local_server):
    result = check_website_url(f"{local_server}/ok")
    assert result.classification == LIKELY_WORKING
    assert result.http_status_code == 200
    assert result.page_title == "Sample show"
    assert "no configured soft-404 signals" in result.evidence


def test_http_404_is_an_http_error(local_server):
    result = check_website_url(f"{local_server}/404")
    assert result.classification == HTTP_ERROR
    assert result.http_status_code == 404


def test_http_200_with_error_content_is_suspected_soft_404(local_server):
    result = check_website_url(f"{local_server}/soft404")
    assert result.classification == SUSPECTED_SOFT_404
    assert result.page_title == "Page Not Found"
    assert "Page Not Found" in result.evidence


def test_long_article_mentioning_error_phrase_is_not_soft_404(local_server):
    result = check_website_url(f"{local_server}/article-mentions-error")
    assert result.classification == LIKELY_WORKING

def test_redirect_to_working_page_is_recorded_and_final_page_classified(local_server):
    result = check_website_url(f"{local_server}/redirect-working")
    assert result.classification == LIKELY_WORKING
    assert result.redirect_count == 1
    assert result.redirect_chain[0].status_code == 302
    assert result.redirect_chain[0].destination_url.endswith("/ok")
    assert result.final_url.endswith("/ok")


def test_redirect_to_http_error_is_not_itself_a_failure_classification(local_server):
    result = check_website_url(f"{local_server}/redirect-error")
    assert result.classification == HTTP_ERROR
    assert result.http_status_code == 404
    assert result.redirect_count == 1
    assert result.final_url.endswith("/404")


def test_redirect_to_private_address_is_blocked_before_request(local_server):
    result = check_website_url(f"{local_server}/redirect-private")
    assert result.classification == INCONCLUSIVE
    assert result.error_reason == "prohibited_ip_destination"
    assert result.redirect_count == 1
    assert result.final_url.endswith("/redirect-private")


def test_read_timeout_is_inconclusive(local_server):
    started = time.monotonic()
    result = check_website_url(
        f"{local_server}/slow",
        UrlCheckerConfig(connect_timeout_seconds=1, request_timeout_seconds=0.05),
    )
    assert result.classification == INCONCLUSIVE
    assert result.error_reason == "timeout"
    assert time.monotonic() - started < 0.5


def test_total_timeout_aborts_slow_stream(local_server):
    started = time.monotonic()
    result = check_website_url(
        f"{local_server}/slow-stream",
        UrlCheckerConfig(connect_timeout_seconds=1, request_timeout_seconds=0.12),
    )
    assert result.classification == INCONCLUSIVE
    assert result.error_reason == "timeout"
    assert time.monotonic() - started < 0.5


def test_connection_failure_is_inconclusive(monkeypatch):
    monkeypatch.setattr(url_checker, "_resolve_host", lambda _host, _port, _timeout: ["8.8.8.8"])
    class FailingSession:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def get(self, *_args, **_kwargs): raise url_checker.RequestException("controlled failure")

    monkeypatch.setattr(url_checker, "CurlSession", FailingSession)
    result = check_website_url("https://podcast.example/unavailable")
    assert result.classification == INCONCLUSIVE
    assert result.error_reason == "connection_error"


def test_invalid_input_does_not_make_a_request():
    result = check_website_url("example.test/path")
    assert result.classification == INVALID_INPUT
    assert result.error_reason == "malformed_url"
    assert result.http_status_code is None


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.10.20/",
        "http://192.168.1.2/",
        "http://[::1]/",
    ],
)
def test_prohibited_destinations_are_rejected(url):
    result = check_website_url(url)
    assert result.classification == INCONCLUSIVE
    assert result.error_reason in {"prohibited_hostname", "prohibited_ip_destination"}
    assert result.http_status_code is None


def test_hostname_with_mixed_public_and_private_dns_answers_is_rejected(monkeypatch):
    monkeypatch.setattr(url_checker, "_resolve_host", lambda _host, _port, _timeout: ["8.8.8.8", "10.0.0.4"])
    result = check_website_url("https://podcast.example/website")
    assert result.classification == INCONCLUSIVE
    assert result.error_reason == "prohibited_ip_destination"
    assert result.http_status_code is None


def test_https_scheme_is_accepted_by_url_safety_validation(monkeypatch):
    monkeypatch.setattr(url_checker, "_resolve_host", lambda _host, _port, _timeout: ["8.8.8.8"])
    normalized, safety_error, input_error = url_checker._validate_destination("https://podcast.example/")
    assert normalized == "https://podcast.example/"
    assert safety_error is None
    assert input_error is None


def test_dns_result_is_pinned_and_original_hostname_keeps_tls_verification(monkeypatch):
    captured = {}
    monkeypatch.setattr(url_checker, "_resolve_host", lambda *_args: ["8.8.8.8"])

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}

    class Session:
        def __init__(self, **kwargs):
            captured["session"] = kwargs
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def get(self, url, **kwargs):
            captured["url"] = url
            captured["request"] = kwargs
            kwargs["content_callback"](b"<title>Safe</title>")
            return Response()

    monkeypatch.setattr(url_checker, "CurlSession", Session)
    result = check_website_url("https://podcast.example/path")

    assert result.classification == LIKELY_WORKING
    assert captured["url"] == "https://podcast.example/path"
    options = captured["session"]["curl_options"]
    assert options[url_checker.CurlOpt.RESOLVE] == ["podcast.example:443:8.8.8.8"]
    assert options[url_checker.CurlOpt.SSL_VERIFYPEER] == 1
    assert options[url_checker.CurlOpt.SSL_VERIFYHOST] == 2
    assert captured["session"]["verify"] is True
    assert captured["session"]["trust_env"] is False
    assert captured["session"]["allow_redirects"] is False


def test_certificate_validation_failure_is_safely_classified(monkeypatch):
    monkeypatch.setattr(url_checker, "_resolve_host", lambda *_args: ["8.8.8.8"])

    class Session:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def get(self, *_args, **_kwargs):
            raise url_checker.CertificateVerifyError("raw host and certificate details")

    monkeypatch.setattr(url_checker, "CurlSession", Session)
    result = check_website_url("https://podcast.example/private-token")
    assert result.classification == INCONCLUSIVE
    assert result.error_reason == "tls_verification_failed"
    assert "podcast.example" not in result.evidence
    assert "private-token" not in result.evidence
    assert "raw host" not in result.evidence


def test_late_dns_rebinding_cannot_change_the_pinned_connection(local_server, monkeypatch):
    answers = 0

    def changing_answers(*_args):
        nonlocal answers
        answers += 1
        return ["127.0.0.1"] if answers == 1 else ["10.0.0.9"]

    monkeypatch.setattr(url_checker, "_resolve_host", changing_answers)
    monkeypatch.setattr(url_checker, "_is_public_address", lambda address: address == "127.0.0.1")
    result = check_website_url(f"{local_server}/ok")

    assert result.classification == LIKELY_WORKING
    assert answers == 1


def test_expired_stage_deadline_starts_no_request(monkeypatch):
    monkeypatch.setattr(url_checker, "_resolve_host", lambda *_args: pytest.fail("DNS should not run"))
    result = check_website_url(
        "https://podcast.example/",
        deadline_monotonic=time.monotonic() - 1,
    )
    assert result.classification == INCONCLUSIVE
    assert result.error_reason == "check_budget_exhausted"


@pytest.mark.parametrize("path", ["/large", "/large-streamed"])
def test_oversized_response_is_inconclusive(local_server, path):
    result = check_website_url(
        f"{local_server}{path}",
        UrlCheckerConfig(max_response_bytes=128),
    )
    assert result.classification == INCONCLUSIVE
    assert result.error_reason == "response_too_large"






