import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.database import initialize_database, persist_audit_run
from src.url_checker import (
    HTTP_ERROR,
    INCONCLUSIVE,
    INVALID_INPUT,
    LIKELY_WORKING,
    SUSPECTED_SOFT_404,
    RedirectHop,
    UrlCheckResult,
)
from src.validation import ValidatedRecord, ValidationIssue

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = PROJECT_ROOT / "sql"


def run_query(connection, name):
    return connection.execute((SQL_DIR / name).read_text(encoding="utf-8-sig")).fetchall()


def make_record(row, feed_id, url, issues=()):
    original = {"feed_id": feed_id, "title": f"Show {row}", "website_url": url}
    normalized = {**original, "normalized_website_url": url.strip() if url else None}
    return ValidatedRecord(row, original, normalized, tuple(issues))


def result(url, classification, status=None, redirects=()):
    return UrlCheckResult(
        original_url=url,
        final_url=url,
        http_status_code=status,
        redirect_count=len(redirects),
        redirect_chain=tuple(redirects),
        response_time_ms=1.0,
        classification=classification,
        evidence=f"Controlled {classification} fixture.",
        error_reason="timeout" if classification == INCONCLUSIVE else None,
    )


def populate_controlled_database(path):
    duplicate = ValidationIssue("duplicate_feed_id", "Duplicate feed ID.", "feed_id")
    blank_url = ValidationIssue("blank_website_url", "URL is blank.", "website_url")
    optional = ValidationIssue("missing_optional_field", "Language is missing.", "language", "warning")
    schema_issue = ValidationIssue("missing_required_column", "Column is missing.", "language")
    first_records = [
        make_record(2, "feed-a", "https://a.example"),
        make_record(3, "dup", "https://b.example", [duplicate]),
        make_record(4, "dup", "", [duplicate, blank_url]),
    ]
    redirect = RedirectHop("https://a.example", 301, "https://www.a.example/")
    persist_audit_run(
        path, "first.csv", first_records, audit_run_id="run-1",
        started_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        schema_issues=[schema_issue],
        url_check_results={
            2: result("https://a.example", LIKELY_WORKING, 200, [redirect]),
            3: result("https://b.example", HTTP_ERROR, 404),
            4: result("", INVALID_INPUT),
        },
    )
    second_records = [
        make_record(2, "feed-a", "https://a.example"),
        make_record(3, "feed-e", "https://e.example", [optional]),
    ]
    persist_audit_run(
        path, "second.csv", second_records, audit_run_id="run-2",
        started_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        url_check_results={
            2: result("https://a.example", SUSPECTED_SOFT_404, 200),
            3: result("https://e.example", INCONCLUSIVE),
        },
    )


def test_all_analytics_queries_execute_against_empty_database(tmp_path):
    path = tmp_path / "empty.sqlite"
    initialize_database(path)
    query_files = sorted(SQL_DIR.glob("*.sql"))
    assert len(query_files) >= 8
    with sqlite3.connect(path) as connection:
        for query in query_files:
            rows = connection.execute(query.read_text(encoding="utf-8-sig")).fetchall()
            assert rows == [], query.name


def test_run_counts_issues_and_issue_rates_use_correct_denominators(tmp_path):
    path = tmp_path / "analytics.sqlite"
    initialize_database(path)
    populate_controlled_database(path)
    with sqlite3.connect(path) as connection:
        run_rows = run_query(connection, "run_record_counts.sql")
        assert [(row[0], row[5], row[6]) for row in run_rows] == [
            ("run-1", 3, 3), ("run-2", 2, 2)
        ]
        issues = run_query(connection, "validation_issue_counts.sql")
        by_category = {(row[0], row[2]): row[4] for row in issues}
        assert by_category[("run-1", "duplicate_feed_id")] == 2
        assert by_category[("run-1", "missing_required_column")] == 1
        rates = run_query(connection, "record_issue_rates.sql")
        by_run = {row[0]: row for row in rates}
        assert by_run["run-1"][2:5] == (3, 2, 66.67)
        assert by_run["run-1"][5] == 1  # Schema issue is reported separately.
        assert by_run["run-2"][2:5] == (2, 1, 50.0)


def test_url_outcomes_status_redirects_and_over_time_queries(tmp_path):
    path = tmp_path / "analytics.sqlite"
    initialize_database(path)
    populate_controlled_database(path)
    with sqlite3.connect(path) as connection:
        outcomes = run_query(connection, "url_outcomes_by_classification.sql")
        classes = {(row[0], row[1]): (row[2], row[4]) for row in outcomes}
        assert classes[("run-1", LIKELY_WORKING)] == (1, 33.33)
        assert classes[("run-1", INVALID_INPUT)] == (1, 33.33)
        assert classes[("run-2", INCONCLUSIVE)] == (1, 50.0)

        statuses = run_query(connection, "http_status_distribution.sql")
        status_counts = {(row[0], row[1]): row[2] for row in statuses}
        assert status_counts[("run-1", "no_http_response")] == 1
        assert status_counts[("run-1", "404")] == 1

        redirects = run_query(connection, "redirect_frequency.sql")
        redirect_run = next(row for row in redirects if row[0] == "run-1")
        assert redirect_run[2:4] == (1, 1)
        destinations = run_query(connection, "redirect_destinations.sql")
        assert destinations[0][3] == "https://www.a.example/"

        trends = run_query(connection, "url_outcomes_over_time.sql")
        likely = [row for row in trends if row[3] == LIKELY_WORKING]
        assert [row[5] for row in likely] == [1, 0]
        assert likely[1][8] == -33.33

        comparisons = run_query(connection, "audit_run_comparison.sql")
        assert comparisons[0][5:8] == (3, 2, 66.67)
        assert comparisons[1][11] == -1


def test_missing_website_and_duplicate_feed_queries(tmp_path):
    path = tmp_path / "analytics.sqlite"
    initialize_database(path)
    populate_controlled_database(path)
    with sqlite3.connect(path) as connection:
        missing = run_query(connection, "missing_website_urls.sql")
        by_run = {row[0]: row for row in missing}
        assert by_run["run-1"][1:4] == (3, 1, 33.33)
        assert by_run["run-2"][1:4] == (2, 0, 0.0)
        duplicates = run_query(connection, "duplicate_feed_ids.sql")
        assert [(row[0], row[1], row[2]) for row in duplicates] == [("run-1", "dup", 2)]



