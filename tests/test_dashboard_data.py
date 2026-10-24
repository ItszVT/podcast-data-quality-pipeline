from __future__ import annotations

from pathlib import Path

import pytest

from src import dashboard_data as dashboard
from src.database import persist_audit_run
from src.url_checker import (
    HTTP_ERROR, INCONCLUSIVE, LIKELY_WORKING, RedirectHop, UrlCheckResult,
)
from src.validation import ValidatedRecord, ValidationIssue


def record(row: int, feed_id: str | None, *, title: str = "Show", url: str | None = "https://show.test", issues=()):
    original = {"feed_id": feed_id, "title": title, "website_url": url}
    normalized = {"feed_id": feed_id, "title": title, "website_url": url,
                  "normalized_website_url": url}
    return ValidatedRecord(row, original, normalized, tuple(issues))


def check(url: str, classification: str, *, status: int | None = 200, redirect_to: str | None = None):
    hops = (RedirectHop(url, 302, redirect_to),) if redirect_to else ()
    return UrlCheckResult(
        original_url=url, final_url=redirect_to or url, http_status_code=status,
        redirect_count=len(hops), redirect_chain=hops, response_time_ms=10.0,
        classification=classification, evidence="controlled test result",
        error_reason="timeout" if classification == INCONCLUSIVE else None,
    )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "audits.sqlite"


def test_empty_database_returns_zero_history_and_empty_views(database_path):
    persist_audit_run(database_path, "empty.csv", [], audit_run_id="empty")
    assert dashboard.get_history_summary(database_path) == {"audit_runs": 1, "records_processed": 0}
    summary = dashboard.get_run_summary(database_path, "empty")
    assert summary["stored_records"] == 0
    assert summary["records_with_issues_pct"] is None
    assert dashboard.get_issue_counts(database_path, "empty") == []
    assert dashboard.get_url_outcomes(database_path, "empty") == []
    redirects = dashboard.get_redirect_summary(database_path, "empty")
    assert redirects["records_with_redirects"] == 0
    assert redirects["redirect_hops"] == 0


def test_run_views_count_rows_issues_missing_urls_and_url_outcomes(database_path):
    shared_issues = (
        ValidationIssue("malformed_url", "Not an absolute URL", "website_url"),
        ValidationIssue("missing_optional_field", "No description", "description", "warning"),
    )
    records = [
        record(2, "dup", title="First", url=" ", issues=shared_issues),
        record(3, "dup", title="Second", url=None,
               issues=(ValidationIssue("duplicate_feed_id", "Repeated ID", "feed_id"),)),
        record(4, "good", url="https://show.test"),
    ]
    checks = {
        2: check("https://show.test", LIKELY_WORKING, redirect_to="https://www.show.test"),
        3: check("https://second.test", HTTP_ERROR, status=404),
        4: check("https://third.test", INCONCLUSIVE, status=None),
    }
    persist_audit_run(database_path, "shows.csv", records, audit_run_id="run-1", url_check_results=checks)

    summary = dashboard.get_run_summary(database_path, "run-1")
    assert summary["stored_records"] == 3
    assert summary["records_with_issues"] == 2
    assert summary["issue_occurrences"] == 3
    assert summary["records_with_issues_pct"] == pytest.approx(66.67)
    assert summary["missing_website_urls"] == 2
    issue_rows = dashboard.get_issue_counts(database_path, "run-1")
    assert {row["category"] for row in issue_rows} == {"malformed_url", "missing_optional_field", "duplicate_feed_id"}
    affected = dashboard.get_affected_records(database_path, "run-1")
    assert len(affected) == 2
    assert next(row for row in affected if row["feed_id"] == "dup")["issue_count"] == 2
    assert dashboard.get_duplicate_feed_ids(database_path, "run-1")[0]["feed_id"] == "dup"
    assert {row["classification"] for row in dashboard.get_url_outcomes(database_path, "run-1")} == {LIKELY_WORKING, HTTP_ERROR, INCONCLUSIVE}
    assert dashboard.get_http_status_distribution(database_path, "run-1")
    redirects = dashboard.get_redirect_summary(database_path, "run-1")
    assert redirects["records_with_redirects"] == 1
    assert redirects["redirect_hops"] == 1
    assert dashboard.get_redirect_destinations(database_path, "run-1")[0]["destination_url"] == "https://www.show.test"
    assert len(dashboard.get_url_check_rows(database_path, "run-1", classification=INCONCLUSIVE)) == 1


def test_multiple_run_comparison_only_matches_unique_identifiers_and_checked_pairs(database_path):
    first = [
        record(2, "same", title="Before", url="https://same.test"),
        record(3, "removed"),
        record(4, "ambiguous"),
        record(5, "ambiguous"),
        record(6, None),
        record(7, "unchecked"),
    ]
    second = [
        record(2, "same", title="After", url="https://same.test"),
        record(3, "added"),
        record(4, "ambiguous"),
        record(5, None),
        record(6, "unchecked"),
    ]
    persist_audit_run(database_path, "first.csv", first, audit_run_id="first", url_check_results={2: check("https://same.test", LIKELY_WORKING), 7: check("https://unchecked.test", LIKELY_WORKING)})
    persist_audit_run(database_path, "second.csv", second, audit_run_id="second", url_check_results={2: check("https://same.test", HTTP_ERROR, status=500)})

    assert [run["audit_run_id"] for run in dashboard.get_runs(database_path)] == ["second", "first"]
    assert dashboard.get_history_summary(database_path) == {"audit_runs": 2, "records_processed": 11}
    comparison = dashboard.compare_runs(database_path, "first", "second")
    assert comparison["present_in_both"][0]["url_outcome_status"] == "changed"
    assert comparison["newly_observed"][0]["feed_id"] == "added"
    assert comparison["absent_from_later"][0]["feed_id"] == "removed"
    assert comparison["ambiguous_identifiers"][0]["feed_id"] == "ambiguous"
    assert comparison["unidentified_records"]
    assert next(item for item in comparison["present_in_both"] if item["feed_id"] == "unchecked")["url_outcome_status"] == "not_checked_in_both"
    assert len(dashboard.get_history_metrics(database_path)) == 2
    assert dashboard.get_url_outcome_trends(database_path)


def test_dashboard_opens_existing_database_read_only(database_path):
    persist_audit_run(database_path, "shows.csv", [record(2, "one")], audit_run_id="run")
    before = database_path.read_bytes()
    assert dashboard.get_runs(database_path)
    assert database_path.read_bytes() == before


def test_missing_database_is_not_created_and_bad_schema_is_reported_safely(tmp_path):
    missing = tmp_path / "absent.sqlite"
    with pytest.raises(dashboard.DashboardDataError, match="not found"):
        dashboard.get_runs(missing)
    assert not missing.exists()

    wrong_schema = tmp_path / "wrong.sqlite"
    import sqlite3
    with sqlite3.connect(wrong_schema) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(dashboard.DashboardDataError, match="expected Podcast Pipeline schema"):
        dashboard.get_runs(wrong_schema)


def test_record_filters_and_field_level_finding_counts(database_path):
    records = [
        record(2, "one", title="One", url="", issues=(
            ValidationIssue("blank_website_url", "No publisher site.", "website_url"),
            ValidationIssue("missing_optional_field", "No country.", "country", "warning"),
        )),
        record(3, "two", title="Two", url="https://two.test", issues=(
            ValidationIssue("missing_optional_field", "No country.", "country", "warning"),
        )),
        record(4, "three", title="Three", url="https://three.test"),
    ]
    persist_audit_run(database_path, "shows.csv", records, audit_run_id="filters")

    counts = dashboard.get_issue_field_counts(database_path, "filters")
    country = next(row for row in counts if row["field"] == "country")
    assert country["issue_count"] == 2
    assert country["affected_record_count"] == 2
    assert len(dashboard.get_podcast_records(database_path, "filters")) == 3
    assert len(dashboard.get_podcast_records(database_path, "filters", filter_mode="with_findings")) == 2
    assert [row["title"] for row in dashboard.get_podcast_records(database_path, "filters", filter_mode="without_findings")] == ["Three"]
    assert [row["title"] for row in dashboard.get_podcast_records(database_path, "filters", filter_mode="missing_website")] == ["One"]
    assert [row["title"] for row in dashboard.get_podcast_records(database_path, "filters", filter_mode="category", issue_category="missing_optional_field")] == ["One", "Two"]
    assert [row["title"] for row in dashboard.get_podcast_records(database_path, "filters", search="three")] == ["Three"]
    field_records = dashboard.get_affected_records(database_path, "filters", issue_category="missing_optional_field", issue_field="country")
    assert [row["title"] for row in field_records] == ["One", "Two"]
    assert dashboard.get_record_findings(database_path, "filters", field_records[0]["record_id"])


def test_podcast_record_filters_reject_unknown_mode(database_path):
    persist_audit_run(database_path, "empty.csv", [], audit_run_id="empty-filter")
    with pytest.raises(ValueError, match="Unsupported podcast record filter"):
        dashboard.get_podcast_records(database_path, "empty-filter", filter_mode="not-a-filter")


def test_run_summary_exposes_sanitized_website_check_stage_state(tmp_path):
    database_path = tmp_path / "website-stage.sqlite"
    persist_audit_run(
        database_path,
        "one.csv",
        [record(2, "one", url="https://one.test")],
        audit_run_id="partial-checks",
        metadata={"website_checking": {
            "status": "partial", "unique_urls_checked": 4,
            "unexpected_error_count": 1,
        }},
    )

    summary = dashboard.get_run_summary(database_path, "partial-checks")
    assert summary["website_check_status"] == "partial"
    assert summary["website_check_unique_urls"] == 4
    assert summary["website_check_unexpected_errors"] == 1
