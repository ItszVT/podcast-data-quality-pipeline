"""Fail-closed validation for the SQLite file published by scheduled audits."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

SNAPSHOT_RELATIVE_PATH = "data/real_podcast_audits.sqlite"
# The collector requests up to 10 results per search term by default.
MIN_PARTIAL_PUBLISHABLE_RECORDS = 10

_REQUIRED_COLUMNS = {
    "audit_runs": {
        "audit_run_id", "started_at", "completed_at", "input_filename",
        "run_status", "records_processed", "metadata_json",
    },
    "podcast_records": {
        "record_id", "audit_run_id", "source_row", "feed_id", "title",
        "website_url", "normalized_website_url", "record_json",
    },
    "data_quality_issues": {
        "issue_id", "audit_run_id", "record_id", "category", "severity", "field", "message",
    },
    "url_checks": {
        "url_check_id", "audit_run_id", "record_id", "classification",
        "http_status_code", "original_url", "final_url", "result_json",
    },
}


class SnapshotValidationError(ValueError):
    """The SQLite file does not contain a complete, publishable real audit."""


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_recoverable_rss_partial(
    metadata: dict[str, object], records_processed: int, stored_count: int
) -> bool:
    """Allow only well-formed partial runs with healthy search and limited RSS loss."""
    stages = metadata.get("stages")
    report = metadata.get("collection_report")
    if not isinstance(stages, dict) or not isinstance(report, dict):
        return False
    if any(stages.get(name) != "succeeded" for name in ("preflight", "validation", "persistence")):
        return False

    api_errors = report.get("api_search_errors")
    rss_errors = report.get("rss_errors")
    rss_successes = report.get("rss_retrieval_successes")
    rss_failures = report.get("rss_retrieval_failures")
    missing_feed_urls = report.get("missing_rss_feed_urls")
    unique_records = report.get("unique_records_retained")
    collected_records = metadata.get("records_collected")
    validated_records = metadata.get("records_validated")

    if not isinstance(api_errors, list) or api_errors:
        return False
    if not isinstance(rss_errors, list) or not all(isinstance(item, str) for item in rss_errors):
        return False
    counts = (
        rss_successes, rss_failures, missing_feed_urls, unique_records,
        collected_records, validated_records,
    )
    if not all(_is_count(value) for value in counts):
        return False
    if (
        rss_failures < 1
        or rss_successes < 1
        or rss_failures >= rss_successes
        or len(rss_errors) != rss_failures
        or records_processed < MIN_PARTIAL_PUBLISHABLE_RECORDS
        or stored_count != records_processed
        or unique_records != records_processed
        or collected_records != records_processed
        or validated_records != records_processed
        or rss_successes + rss_failures + missing_feed_urls != records_processed
    ):
        return False
    return True


def validate_publishable_snapshot(
    path: str | Path | bytes, *, allow_recoverable_rss_partial: bool = False
) -> None:
    """Require a healthy schema and completed successful or eligible RSS-partial audit."""
    try:
        if isinstance(path, bytes):
            if not path.startswith(b"SQLite format 3\x00"):
                raise SnapshotValidationError("The real audit database is missing or invalid.")
            connection = sqlite3.connect(":memory:")
            connection.deserialize(path)
        else:
            database_path = Path(path)
            if not database_path.is_file():
                raise SnapshotValidationError("The real audit database is missing.")
            uri = database_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)

        with closing(connection):
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise SnapshotValidationError("The real audit database failed its integrity check.")

            for table, required_columns in _REQUIRED_COLUMNS.items():
                actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not required_columns.issubset(actual):
                    raise SnapshotValidationError("The real audit database schema is incomplete.")

            latest = connection.execute(
                """SELECT audit_run_id, run_status, records_processed, metadata_json, completed_at
                   FROM audit_runs ORDER BY started_at DESC, audit_run_id DESC LIMIT 1"""
            ).fetchone()
            if (
                latest is None
                or not isinstance(latest[4], str)
                or not latest[4].strip()
                or latest[1] not in {"succeeded", "partial"}
                or not _is_count(latest[2])
                or latest[2] < 1
            ):
                raise SnapshotValidationError("The latest real audit is not a completed nonempty run.")

            try:
                metadata = json.loads(latest[3])
            except (TypeError, json.JSONDecodeError) as exc:
                raise SnapshotValidationError("The latest real audit metadata is invalid.") from exc

            stages = metadata.get("stages") if isinstance(metadata, dict) else None
            if not isinstance(stages, dict) or metadata.get("pipeline") != "daily_collection":
                raise SnapshotValidationError("The latest real audit metadata is invalid.")

            if latest[1] == "succeeded":
                if any(
                    stages.get(name) != "succeeded"
                    for name in ("preflight", "collection", "validation", "persistence")
                ):
                    raise SnapshotValidationError("The latest real audit did not pass all required stages.")
            elif (
                not allow_recoverable_rss_partial
                or stages.get("collection") != "partial"
            ):
                raise SnapshotValidationError("The latest real audit is not an eligible RSS-only partial run.")

            stored_count = connection.execute(
                "SELECT COUNT(*) FROM podcast_records WHERE audit_run_id = ?", (latest[0],)
            ).fetchone()[0]
            if stored_count != latest[2] or stored_count < 1:
                raise SnapshotValidationError("The latest real audit record count is inconsistent.")
            if latest[1] == "partial" and not _is_recoverable_rss_partial(
                metadata, latest[2], stored_count
            ):
                raise SnapshotValidationError("The latest real audit is not an eligible RSS-only partial run.")
    except SnapshotValidationError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise SnapshotValidationError("The real audit database could not be verified.") from exc
