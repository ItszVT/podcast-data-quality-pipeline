from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src import url_checker
from src.evaluation import evaluate_reference_dataset
from src.url_checker import INCONCLUSIVE

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReferenceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/404":
            self._send(404, "<html><title>Missing</title><h1>Missing</h1></html>")
        elif self.path == "/soft404":
            self._send(200, "<html><title>Page Not Found</title><h1>Page Not Found</h1></html>")
        elif self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.2:{self.server.server_port}/blocked")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(200, "<html><title>Podcast</title><h1>Welcome</h1></html>")

    def _send(self, status, body):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def controlled_reference_server(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReferenceHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_resolver = url_checker._resolve_host

    def permit_only_fixture_listener(host, port):
        if host == "127.0.0.1" and port == server.server_port:
            # Only the safety lookup is narrowed to this fixture's bound port;
            # Requests still makes its real request to the loopback test server.
            return ["93.184.216.34"]
        return real_resolver(host, port)

    monkeypatch.setattr(url_checker, "_resolve_host", permit_only_fixture_listener)
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_reference_evaluation_uses_checker_and_keeps_inconclusive_distinct(controlled_reference_server):
    report = evaluate_reference_dataset(
        PROJECT_ROOT / "data" / "reference_url_outcomes.csv",
        base_url=controlled_reference_server,
    )
    serialized = report.to_dict()

    assert report.case_count == 4
    assert report.correct_count == 4
    assert report.accuracy == 1.0
    assert report.confusion_matrix[INCONCLUSIVE][INCONCLUSIVE] == 1
    assert serialized["cases"][-1]["predicted_classification"] == INCONCLUSIVE
    assert serialized["cases"][-1]["matched"] is True


def test_reference_evaluation_rejects_non_loopback_base_before_checking():
    with pytest.raises(ValueError, match="loopback"):
        evaluate_reference_dataset(base_url="https://example.com")


def test_evaluator_reports_mismatches_without_relabeling_inconclusive():
    from src.evaluation import EvaluationCase, evaluate_url_cases
    from src.url_checker import LIKELY_WORKING, UrlCheckResult

    observed = UrlCheckResult(
        original_url="http://127.0.0.1/unavailable", final_url=None,
        http_status_code=None, redirect_count=0, redirect_chain=(),
        response_time_ms=1.0, classification=INCONCLUSIVE,
        evidence="Controlled no-response fixture.", error_reason="timeout",
    )
    report = evaluate_url_cases(
        [EvaluationCase("known-working", "http://127.0.0.1/ok", LIKELY_WORKING)],
        checker=lambda _url: observed,
    )

    assert report.accuracy == 0.0
    assert report.confusion_matrix[LIKELY_WORKING][INCONCLUSIVE] == 1
    assert "http_error" not in report.confusion_matrix[LIKELY_WORKING]


def test_empty_evaluation_has_no_accuracy():
    from src.evaluation import evaluate_url_cases

    report = evaluate_url_cases([])
    assert report.case_count == 0
    assert report.accuracy is None
    assert report.confusion_matrix == {}

