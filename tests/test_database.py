import json
import sqlite3
from pathlib import Path

import pytest

from src.database import initialize_database, persist_audit_run
from src.url_checker import INCONCLUSIVE, LIKELY_WORKING, UrlCheckResult
from src.validation import ValidatedRecord, ValidationIssue


def valid_record(source_row=2, feed_id="feed-1", url="https://podcast.example"):
    original = {
        "feed_id": feed_id,
        "title": "Example Podcast",
        "website_url": url,
        "description": "A sample show",
        "language": "en",
    }
    normalized = {**original, "normalized_website_url": url}
    return ValidatedRecord(source_row, original, normalized, ())


def db_connection(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def test_database_schema_creation_is_idempotent_and_relational(tmp_path):
    path = tmp_path / "nested" / "podcasts.sqlite"
    initialize_database(path)
    initialize_database(path)

    with db_connection(path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"audit_runs", "podcast_records", "url_checks", "data_quality_issues"} <= tables
        assert "idx_url_checks_classification" in indexes
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_list(podcast_records)").fetchall()
        assert connection.execute("PRAGMA foreign_key_list(url_checks)").fetchall()


def test_persists_record_issues_missing_values_and_failed_url_result(tmp_path):
    path = tmp_path / "audits.sqlite"
    broken_url = UrlCheckResult(
        original_url="https://podcast.example", final_url=None, http_status_code=None,
        redirect_count=0, redirect_chain=(), response_time_ms=22.4,
        classification=INCONCLUSIVE, evidence="The request timed out.", error_reason="timeout",
    )
    record = ValidatedRecord(
        source_row=2,
        original={"feed_id": "feed-1", "title": "Example Podcast", "website_url": "", "description": ""},
        normalized={"feed_id": "feed-1", "title": "Example Podcast", "website_url": "",
                    "normalized_website_url": None, "description": ""},
        issues=(ValidationIssue("blank_website_url", "Website URL is blank.", "website_url"),),
    )
    run_id = persist_audit_run(
        path, "podcasts.csv", [record], audit_run_id="run-1", run_status="partial",
        metadata={"source": "synthetic"},
        url_check_results={2: broken_url},
    )

    with db_connection(path) as connection:
        run = connection.execute("SELECT * FROM audit_runs WHERE audit_run_id=?", (run_id,)).fetchone()
        stored = connection.execute("SELECT * FROM podcast_records WHERE audit_run_id=?", (run_id,)).fetchone()
        issue = connection.execute("SELECT * FROM data_quality_issues WHERE audit_run_id=?", (run_id,)).fetchone()
        check = connection.execute("SELECT * FROM url_checks WHERE audit_run_id=?", (run_id,)).fetchone()
        assert run["input_filename"] == "podcasts.csv" and run["records_processed"] == 1
        assert run["started_at"] and run["completed_at"]
        assert json.loads(run["metadata_json"]) == {"source": "synthetic"}
        assert stored["feed_id"] == "feed-1" and stored["website_url"] == ""
        assert stored["normalized_website_url"] is None
        assert json.loads(stored["record_json"])["original"]["description"] == ""
        assert issue["category"] == "blank_website_url"
        assert check["classification"] == INCONCLUSIVE and check["http_status_code"] is None
        assert json.loads(check["result_json"])["error_reason"] == "timeout"


def test_repeated_runs_retain_separate_history_for_same_input(tmp_path):
    path = tmp_path / "audits.sqlite"
    first = persist_audit_run(path, "same.csv", [valid_record()], audit_run_id="run-a")
    second = persist_audit_run(path, "same.csv", [valid_record()], audit_run_id="run-b")

    with db_connection(path) as connection:
        assert first != second
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 2
        assert {row[0] for row in connection.execute(
            "SELECT audit_run_id FROM podcast_records")} == {"run-a", "run-b"}


def test_duplicate_feed_ids_within_run_are_retained_as_distinct_rows(tmp_path):
    path = tmp_path / "audits.sqlite"
    duplicate_issue = ValidationIssue("duplicate_feed_id", "Duplicate ID.", "feed_id")
    records = [
        ValidatedRecord(2, {"feed_id": "same", "title": "First", "website_url": "https://one.test"},
                        {"normalized_website_url": "https://one.test"}, (duplicate_issue,)),
        ValidatedRecord(3, {"feed_id": "same", "title": "Second", "website_url": "https://two.test"},
                        {"normalized_website_url": "https://two.test"}, (duplicate_issue,)),
    ]
    persist_audit_run(path, "duplicates.csv", records, audit_run_id="duplicate-run")

    with db_connection(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM podcast_records WHERE feed_id='same'").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM data_quality_issues WHERE category='duplicate_feed_id'").fetchone()[0] == 2


def test_duplicate_source_row_rolls_back_entire_run(tmp_path):
    path = tmp_path / "audits.sqlite"
    with pytest.raises(sqlite3.IntegrityError):
        persist_audit_run(path, "bad.csv", [valid_record(2), valid_record(2, "feed-2")],
                          audit_run_id="rolled-back")

    with db_connection(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 0


def test_duplicate_run_id_cannot_overwrite_prior_run(tmp_path):
    path = tmp_path / "audits.sqlite"
    persist_audit_run(path, "first.csv", [valid_record()], audit_run_id="fixed-id")

    with pytest.raises(sqlite3.IntegrityError):
        persist_audit_run(path, "replacement.csv", [valid_record()], audit_run_id="fixed-id")

    with db_connection(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 1
        assert connection.execute("SELECT input_filename FROM audit_runs").fetchone()[0] == "first.csv"
        assert connection.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 1


def test_schema_level_issues_are_attached_to_run_without_a_record(tmp_path):
    path = tmp_path / "audits.sqlite"
    missing_column = ValidationIssue("missing_required_column", "Missing feed_id.", "feed_id")
    persist_audit_run(path, "partial.csv", [], audit_run_id="schema-run", schema_issues=[missing_column])

    with db_connection(path) as connection:
        row = connection.execute("SELECT * FROM data_quality_issues").fetchone()
        assert row["audit_run_id"] == "schema-run"
        assert row["record_id"] is None
        assert row["category"] == "missing_required_column"


def test_url_check_success_result_is_stored_with_http_metadata(tmp_path):
    path = tmp_path / "audits.sqlite"
    result = UrlCheckResult(
        "https://podcast.example", "https://podcast.example/", 200, 0, (), 10.0,
        LIKELY_WORKING, "HTTP 200; no soft 404 signals.", page_title="Podcast",
    )
    persist_audit_run(path, "podcasts.csv", [valid_record()], audit_run_id="checked",
                      url_check_results={2: result})

    with db_connection(path) as connection:
        check = connection.execute("SELECT * FROM url_checks").fetchone()
        assert check["classification"] == LIKELY_WORKING
        assert check["http_status_code"] == 200
        assert json.loads(check["result_json"])["page_title"] == "Podcast"

