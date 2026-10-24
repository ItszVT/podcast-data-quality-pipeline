from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.audit_runner import AuditRunnerError, run_audit
from src.url_checker import LIKELY_WORKING, UrlCheckResult


def write_csv(path: Path, rows: list[list[str]]) -> Path:
    path.write_text(
        "feed_id,title,website_url,description,categories,language,country\n"
        + "".join(",".join(f'\"{cell}\"' for cell in row) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_default_runner_creates_snapshot_without_network_requests(tmp_path):
    input_path = write_csv(tmp_path / "sample.csv", [
        ["pod-1", "One", "https://one.example", "", "", "en", "US"],
        ["pod-2", "Two", "", "", "", "en", "US"],
    ])
    database_path = tmp_path / "audits.sqlite"

    def unexpected_request(_url):
        raise AssertionError("The default audit must not make website requests.")

    result = run_audit(input_path, database_path, checker=unexpected_request)

    assert result.total_rows == 2
    assert result.website_checks == 0
    assert result.rows_with_issues == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM url_checks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0] > 0


def test_runner_appends_history_to_compatible_database(tmp_path):
    input_path = write_csv(tmp_path / "sample.csv", [["pod-1", "One", "https://one.example", "d", "c", "en", "US"]])
    database_path = tmp_path / "audits.sqlite"

    first = run_audit(input_path, database_path)
    second = run_audit(input_path, database_path)

    assert first.audit_run_id != second.audit_run_id
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 2


def test_runner_refuses_incompatible_existing_database_without_modifying_it(tmp_path):
    input_path = write_csv(tmp_path / "sample.csv", [["pod-1", "One", "https://one.example", "d", "c", "en", "US"]])
    database_path = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('preserve me')")
    before = database_path.read_bytes()

    with pytest.raises(AuditRunnerError, match="left unchanged"):
        run_audit(input_path, database_path)

    assert database_path.read_bytes() == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "preserve me"


def test_web_checks_are_opt_in_and_invalid_urls_are_skipped(tmp_path):
    input_path = write_csv(tmp_path / "sample.csv", [
        ["ok", "Valid", "https://one.example", "d", "c", "en", "US"],
        ["bad", "Invalid", "ftp://one.example", "d", "c", "en", "US"],
        ["blank", "Blank", "", "d", "c", "en", "US"],
    ])
    database_path = tmp_path / "audits.sqlite"
    requested: list[str] = []

    def controlled_checker(url: str) -> UrlCheckResult:
        requested.append(url)
        return UrlCheckResult(url, url, 200, 0, (), 1.0, LIKELY_WORKING, "Controlled result")

    result = run_audit(input_path, database_path, check_websites=True, checker=controlled_checker)

    assert requested == ["https://one.example"]
    assert result.website_checks == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM url_checks").fetchone()[0] == 1
