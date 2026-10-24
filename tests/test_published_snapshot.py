from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import requests

from src import dashboard_data
from src.daily_pipeline import run_daily_pipeline
from src.podcast_collector import CollectionResult
from src.published_snapshot import SnapshotValidationError, validate_publishable_snapshot


def record(identifier: str) -> dict[str, str]:
    return {
        "feed_id": identifier,
        "title": f"Show {identifier}",
        "website_url": "https://show.example",
        "description": "An example podcast.",
        "categories": "Education",
        "language": "en",
        "country": "US",
    }


def successful_collection(*_args, **_kwargs) -> CollectionResult:
    return CollectionResult(
        records=(record("itunes:collection:1"),),
        report={"api_search_errors": [], "rss_retrieval_failures": 0},
    )


def rss_partial_collection(*_args, **_kwargs) -> CollectionResult:
    records = tuple(record(f"itunes:collection:{index}") for index in range(10))
    return CollectionResult(
        records=records,
        report={
            "api_search_errors": [],
            "rss_errors": ["itunes:collection:10: Request failed (Timeout)."],
            "rss_retrieval_successes": 9,
            "rss_retrieval_failures": 1,
            "missing_rss_feed_urls": 0,
            "unique_records_retained": 10,
        },
    )


def test_snapshot_restore_and_second_run_preserve_append_only_history(tmp_path: Path):
    first_database = tmp_path / "first.sqlite"
    first = run_daily_pipeline(
        first_database, collector=successful_collection, audit_run_id="first-run"
    )
    assert first.pipeline_status == "succeeded"
    validate_publishable_snapshot(first_database)

    # This mirrors restoring the versioned branch's SQLite blob in a fresh runner.
    restored_database = tmp_path / "restored.sqlite"
    restored_database.write_bytes(first_database.read_bytes())
    second = run_daily_pipeline(
        restored_database, collector=successful_collection, audit_run_id="second-run"
    )
    assert second.pipeline_status == "succeeded"
    validate_publishable_snapshot(restored_database)

    with sqlite3.connect(restored_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM podcast_records").fetchone()[0] == 2
        assert {
            row[0] for row in connection.execute("SELECT audit_run_id FROM audit_runs")
        } == {"first-run", "second-run"}


def test_empty_or_failed_latest_run_is_not_publishable(tmp_path: Path):
    empty_database = tmp_path / "empty.sqlite"
    run_daily_pipeline(
        empty_database,
        collector=lambda *_args, **_kwargs: CollectionResult(
            records=(), report={"api_search_errors": [], "rss_retrieval_failures": 0}
        ),
        audit_run_id="empty-run",
    )
    with pytest.raises(SnapshotValidationError, match="completed nonempty"):
        validate_publishable_snapshot(empty_database)

    failed_database = tmp_path / "failed.sqlite"
    run_daily_pipeline(
        failed_database,
        collector=lambda *_args, **_kwargs: CollectionResult(
            records=(record("one"),),
            report={"api_search_errors": ["temporary failure"], "rss_retrieval_failures": 0},
        ),
        audit_run_id="partial-run",
    )
    with pytest.raises(SnapshotValidationError, match="eligible RSS-only partial"):
        validate_publishable_snapshot(failed_database)


def test_isolated_rss_partial_audit_is_persisted_and_publishable(tmp_path: Path):
    database = tmp_path / "rss-partial.sqlite"
    result = run_daily_pipeline(
        database, collector=rss_partial_collection, audit_run_id="rss-partial-run"
    )

    assert result.pipeline_status == "partial"
    assert result.collection_status == "partial"
    assert result.validation_status == "succeeded"
    assert result.persistence_status == "succeeded"
    assert result.records_collected == result.records_validated == result.records_stored == 10
    with sqlite3.connect(database) as connection:
        run_status, metadata_json = connection.execute(
            "SELECT run_status, metadata_json FROM audit_runs WHERE audit_run_id = ?",
            ("rss-partial-run",),
        ).fetchone()
    metadata = json.loads(metadata_json)
    assert run_status == "partial"
    assert metadata["collection_report"]["rss_retrieval_failures"] == 1
    with pytest.raises(SnapshotValidationError, match="eligible RSS-only partial"):
        validate_publishable_snapshot(database)
    validate_publishable_snapshot(database, allow_recoverable_rss_partial=True)
    assert dashboard_data.get_run_summary(database, "rss-partial-run")["rss_retrieval_failures"] == 1


@pytest.mark.parametrize(
    ("record_count", "rss_successes", "rss_failures", "api_errors"),
    [
        (10, 9, 1, ["one search term failed"]),  # API search was not complete.
        (10, 0, 10, []),  # Every RSS request failed, indicating systemic trouble.
        (9, 8, 1, []),  # Below the meaningful minimum collection size.
    ],
)
def test_nonrecoverable_partial_audits_are_not_publishable(
    tmp_path: Path, record_count: int, rss_successes: int, rss_failures: int, api_errors: list[str]
):
    database = tmp_path / f"ineligible-{record_count}-{rss_successes}.sqlite"
    records = tuple(record(f"itunes:collection:{index}") for index in range(record_count))
    collection_result = CollectionResult(
        records=records,
        report={
            "api_search_errors": api_errors,
            "rss_errors": [f"feed-{index}: timeout" for index in range(rss_failures)],
            "rss_retrieval_successes": rss_successes,
            "rss_retrieval_failures": rss_failures,
            "missing_rss_feed_urls": 0,
            "unique_records_retained": record_count,
        },
    )
    result = run_daily_pipeline(
        database, collector=lambda *_args, **_kwargs: collection_result, audit_run_id="partial-run"
    )
    assert result.pipeline_status == "partial"
    with pytest.raises(SnapshotValidationError, match="eligible RSS-only partial"):
        validate_publishable_snapshot(database, allow_recoverable_rss_partial=True)


@pytest.mark.parametrize("failure_stage", ["collection", "validation", "persistence"])
def test_genuinely_failed_audits_are_not_publishable(tmp_path: Path, failure_stage: str):
    database = tmp_path / f"failed-{failure_stage}.sqlite"

    def collect(*_args, **_kwargs):
        if failure_stage == "collection":
            raise RuntimeError("controlled systemic collection failure")
        return rss_partial_collection()

    def validate(*_args, **_kwargs):
        if failure_stage == "validation":
            raise ValueError("controlled validation failure")
        from src.validation import validate_records

        return validate_records(*_args, **_kwargs)

    def persist(*args, **kwargs):
        if failure_stage == "persistence":
            raise RuntimeError("controlled persistence failure")
        from src.database import persist_audit_run

        return persist_audit_run(*args, **kwargs)

    run_daily_pipeline(
        database,
        collector=collect,
        validator=validate,
        persister=persist,
        audit_run_id=f"failed-{failure_stage}",
    )
    with pytest.raises(SnapshotValidationError):
        validate_publishable_snapshot(database, allow_recoverable_rss_partial=True)


def test_corrupt_database_is_not_publishable(tmp_path: Path):
    database = tmp_path / "corrupt.sqlite"
    database.write_bytes(b"not a SQLite database")
    with pytest.raises(SnapshotValidationError):
        validate_publishable_snapshot(database, allow_recoverable_rss_partial=True)


def test_dashboard_reads_remote_snapshot_bytes_without_modifying_database(tmp_path: Path):
    database = tmp_path / "snapshot.sqlite"
    run_daily_pipeline(database, collector=successful_collection, audit_run_id="saved-run")
    original_bytes = database.read_bytes()

    validate_publishable_snapshot(original_bytes)
    assert dashboard_data.get_runs(original_bytes)[0]["audit_run_id"] == "saved-run"
    assert dashboard_data.get_history_summary(original_bytes) == {
        "audit_runs": 1,
        "records_processed": 1,
    }
    assert database.read_bytes() == original_bytes


def test_github_snapshot_fetch_uses_read_only_authenticated_contents_api(monkeypatch):
    class Response:
        status_code = 200
        headers = {"Content-Length": str(len(b"SQLite format 3\x00snapshot"))}

        def iter_content(self, chunk_size):
            assert chunk_size > 0
            yield b"SQLite format 3\x00snapshot"

        def close(self):
            pass

    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(dashboard_data.requests, "get", fake_get)
    snapshot = dashboard_data.fetch_published_snapshot("read-only-test-token")

    assert snapshot.startswith(b"SQLite format 3\x00")
    assert captured["url"].startswith("https://api.github.com/repos/")
    assert captured["params"] == {"ref": "audit-snapshots"}
    assert captured["headers"]["Accept"] == "application/vnd.github.raw+json"
    assert captured["headers"]["Authorization"] == "Bearer read-only-test-token"
    assert captured["allow_redirects"] is False


def test_github_access_error_does_not_expose_token_or_raw_exception(monkeypatch):
    secret = "read-only-token-that-must-not-appear"

    def fake_get(_url, **_kwargs):
        raise requests.ConnectionError(f"failed for https://private.example/{secret}")

    monkeypatch.setattr(dashboard_data.requests, "get", fake_get)
    with pytest.raises(dashboard_data.DashboardDataError) as error:
        dashboard_data.fetch_published_snapshot(secret)

    assert secret not in str(error.value)
    assert "private.example" not in str(error.value)
    assert "https://" not in str(error.value)


def test_missing_real_database_does_not_fall_back_to_synthetic(tmp_path: Path):
    missing_database = tmp_path / "missing-real.sqlite"
    with pytest.raises(dashboard_data.DashboardDataError, match="Real audit database not found"):
        dashboard_data.get_runs(missing_database)
    assert dashboard_data.DEFAULT_DATABASE_PATH == dashboard_data.REAL_DATABASE_PATH
    assert dashboard_data.DEFAULT_DATABASE_PATH != dashboard_data.SYNTHETIC_DATABASE_PATH

