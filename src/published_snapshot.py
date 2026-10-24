"""Fail-closed validation for the SQLite file published by scheduled audits."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

SNAPSHOT_RELATIVE_PATH = "data/real_podcast_audits.sqlite"

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


def validate_publishable_snapshot(path: str | Path | bytes) -> None:
    """Require a healthy project schema and a successful nonempty daily audit."""
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
                """SELECT audit_run_id, run_status, records_processed, metadata_json
                   FROM audit_runs ORDER BY started_at DESC, audit_run_id DESC LIMIT 1"""
            ).fetchone()
            if (
                latest is None
                or latest[1] != "succeeded"
                or not isinstance(latest[2], int)
                or latest[2] < 1
            ):
                raise SnapshotValidationError("The latest real audit is not a successful nonempty run.")

            try:
                metadata = json.loads(latest[3])
            except (TypeError, json.JSONDecodeError) as exc:
                raise SnapshotValidationError("The latest real audit metadata is invalid.") from exc

            stages = metadata.get("stages") if isinstance(metadata, dict) else None
            if (
                not isinstance(stages, dict)
                or metadata.get("pipeline") != "daily_collection"
                or stages.get("validation") != "succeeded"
            ):
                raise SnapshotValidationError("The latest real audit did not pass validation.")

            stored_count = connection.execute(
                "SELECT COUNT(*) FROM podcast_records WHERE audit_run_id = ?", (latest[0],)
            ).fetchone()[0]
            if stored_count != latest[2] or stored_count < 1:
                raise SnapshotValidationError("The latest real audit record count is inconsistent.")
    except SnapshotValidationError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise SnapshotValidationError("The real audit database could not be verified.") from exc
