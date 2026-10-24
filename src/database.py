"""SQLite persistence for append-only podcast audit runs.

The module stores one snapshot row per source row and run. It intentionally
does not change ingestion, validation, or URL-checking behavior.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .database_config import require_sqlite_backend

SCHEMA_VERSION = 1
_RUN_STATUSES = {"running", "succeeded", "partial", "failed"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_runs (
    audit_run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    input_filename TEXT NOT NULL,
    run_status TEXT NOT NULL CHECK (run_status IN ('running', 'succeeded', 'partial', 'failed')),
    records_processed INTEGER NOT NULL CHECK (records_processed >= 0),
    error_summary TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS podcast_records (
    record_id INTEGER PRIMARY KEY,
    audit_run_id TEXT NOT NULL,
    source_row INTEGER NOT NULL CHECK (source_row > 0),
    feed_id TEXT,
    title TEXT,
    website_url TEXT,
    normalized_website_url TEXT,
    record_json TEXT NOT NULL,
    UNIQUE (audit_run_id, source_row),
    UNIQUE (audit_run_id, record_id),
    FOREIGN KEY (audit_run_id) REFERENCES audit_runs(audit_run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS url_checks (
    url_check_id INTEGER PRIMARY KEY,
    audit_run_id TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    classification TEXT NOT NULL,
    http_status_code INTEGER,
    original_url TEXT,
    final_url TEXT,
    result_json TEXT NOT NULL,
    UNIQUE (audit_run_id, record_id),
    FOREIGN KEY (audit_run_id, record_id)
        REFERENCES podcast_records(audit_run_id, record_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS data_quality_issues (
    issue_id INTEGER PRIMARY KEY,
    audit_run_id TEXT NOT NULL,
    record_id INTEGER,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    field TEXT,
    message TEXT NOT NULL,
    FOREIGN KEY (audit_run_id) REFERENCES audit_runs(audit_run_id) ON DELETE CASCADE,
    FOREIGN KEY (audit_run_id, record_id)
        REFERENCES podcast_records(audit_run_id, record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_audit_runs_started_at ON audit_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_audit_runs_status ON audit_runs(run_status);
CREATE INDEX IF NOT EXISTS idx_podcast_records_feed_id ON podcast_records(feed_id);
CREATE INDEX IF NOT EXISTS idx_podcast_records_run ON podcast_records(audit_run_id);
CREATE INDEX IF NOT EXISTS idx_url_checks_classification ON url_checks(classification);
CREATE INDEX IF NOT EXISTS idx_url_checks_run ON url_checks(audit_run_id);
CREATE INDEX IF NOT EXISTS idx_data_quality_issues_category ON data_quality_issues(category);
CREATE INDEX IF NOT EXISTS idx_data_quality_issues_run ON data_quality_issues(audit_run_id);
"""


def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(database_path).expanduser())
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: str | Path) -> None:
    """Create the database parent folder and schema, safely and idempotently."""
    require_sqlite_backend(database_path)
    path = Path(database_path).expanduser()
    if str(path) == ":memory:":
        raise ValueError("Use a database file path; in-memory databases are connection-scoped here.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        connection.executescript(_SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=_json_value)


def _timestamp(value: str | datetime | None, *, default_now: bool = False) -> str | None:
    if value is None:
        return datetime.now(timezone.utc).isoformat() if default_now else None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return {}


def _record_parts(record: Any, index: int) -> tuple[int, Mapping[str, Any], Mapping[str, Any], Any]:
    try:
        source_row = int(_get(record, "source_row", index + 2))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Record {index + 1} has a non-integer source_row.") from exc
    original_value = _get(record, "original")
    if original_value is None:
        original_value = _get(record, "original_record")
    original = _as_mapping(record if original_value is None else original_value)
    original.pop("source_row", None)
    normalized_value = _get(record, "normalized")
    if normalized_value is None:
        normalized_value = _get(record, "normalized_record", original)
    normalized = _as_mapping(normalized_value)
    issues = _get(record, "issues", _get(record, "validation_issues", ()))
    return source_row, original, normalized, issues or ()


def _issue_values(issue: Any) -> tuple[str, str, str | None, str]:
    category = _get(issue, "category")
    severity = _get(issue, "severity", "error")
    field = _get(issue, "field")
    message = _get(issue, "message")
    if not category or message is None:
        raise ValueError("Each validation issue must include a category and message.")
    return str(category), str(severity), None if field is None else str(field), str(message)


def _url_result_values(result: Any) -> tuple[str, int | None, str | None, str | None, str]:
    classification = _get(result, "classification")
    if not classification:
        raise ValueError("Each URL-check result must include a classification.")
    status = _get(result, "http_status_code")
    if status is not None:
        try:
            status = int(status)
        except (TypeError, ValueError) as exc:
            raise ValueError("URL-check http_status_code must be an integer or None.") from exc
    original_url = _get(result, "original_url")
    final_url = _get(result, "final_url")
    return (str(classification), status,
            None if original_url is None else str(original_url),
            None if final_url is None else str(final_url), _json_dumps(result))


def persist_audit_run(
    database_path: str | Path,
    input_filename: str,
    records: Iterable[Any],
    *,
    audit_run_id: str | None = None,
    started_at: str | datetime | None = None,
    completed_at: str | datetime | None = None,
    run_status: str = "succeeded",
    error_summary: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    schema_issues: Iterable[Any] = (),
    url_check_results: Mapping[int, Any] | None = None,
) -> str:
    """Atomically save a run, source rows, validation issues, and URL results.

    ``records`` accepts mappings or existing ``ValidatedRecord`` objects.
    Mappings may include ``source_row``, ``original``, ``normalized``, and
    ``validation_issues``/``issues``. URL results may be keyed by source row or
    included in a record as ``url_check_result``. Failed checks and missing
    values are retained; source data is never mutated.
    """
    if run_status not in _RUN_STATUSES:
        raise ValueError(f"run_status must be one of: {', '.join(sorted(_RUN_STATUSES))}.")
    if not input_filename:
        raise ValueError("input_filename must not be blank.")
    require_sqlite_backend(database_path)

    checks = url_check_results or {}
    prepared = []
    for index, record in enumerate(records):
        row, original, normalized, issues = _record_parts(record, index)
        result = checks.get(row, _get(record, "url_check_result"))
        prepared.append((row, original, normalized, issues, result))

    run_id = audit_run_id or str(uuid.uuid4())
    started = _timestamp(started_at, default_now=True)
    completed = _timestamp(completed_at, default_now=run_status != "running")
    initialize_database(database_path)
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO audit_runs
               (audit_run_id, started_at, completed_at, input_filename, run_status,
                records_processed, error_summary, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, started, completed, str(input_filename), run_status, len(prepared),
             error_summary, _json_dumps(dict(metadata or {}))),
        )
        for row, original, normalized, issues, result in prepared:
            cursor = connection.execute(
                """INSERT INTO podcast_records
                   (audit_run_id, source_row, feed_id, title, website_url,
                    normalized_website_url, record_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, row, original.get("feed_id"), original.get("title"),
                 original.get("website_url"), normalized.get("normalized_website_url"),
                 _json_dumps({"original": original, "normalized": normalized})),
            )
            record_id = cursor.lastrowid
            for issue in issues:
                category, severity, field, message = _issue_values(issue)
                connection.execute(
                    """INSERT INTO data_quality_issues
                       (audit_run_id, record_id, category, severity, field, message)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (run_id, record_id, category, severity, field, message),
                )
            if result is not None:
                classification, status, original_url, final_url, details = _url_result_values(result)
                connection.execute(
                    """INSERT INTO url_checks
                       (audit_run_id, record_id, classification, http_status_code,
                        original_url, final_url, result_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, record_id, classification, status, original_url, final_url, details),
                )
        for issue in schema_issues:
            category, severity, field, message = _issue_values(issue)
            connection.execute(
                """INSERT INTO data_quality_issues
                   (audit_run_id, record_id, category, severity, field, message)
                   VALUES (?, NULL, ?, ?, ?, ?)""",
                (run_id, category, severity, field, message),
            )
        connection.commit()
        return run_id
    except BaseException:
        # Explicitly roll back even for KeyboardInterrupt/SystemExit. Closing a
        # SQLite connection also rolls back, but making it explicit protects
        # the transaction boundary before the connection is released.
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()

