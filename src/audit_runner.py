"""Command-line entry point for a local podcast audit.

Website requests are disabled by default. An existing database is only appended
when it already has the project's supported schema and version.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from .database import SCHEMA_VERSION, persist_audit_run
from .ingestion import IngestionError, load_podcast_file
from .url_checker import UrlCheckResult, check_website_url
from .validation import ValidationReport, validate_records

DEFAULT_INPUT_PATH = Path("data/sample_podcasts.csv")
DEFAULT_DATABASE_PATH = Path("data/podcast_audits.sqlite")
_REQUIRED_TABLE_COLUMNS = {
    "audit_runs": {"audit_run_id", "started_at", "completed_at", "input_filename", "run_status", "records_processed", "metadata_json"},
    "podcast_records": {"record_id", "audit_run_id", "source_row", "feed_id", "title", "website_url", "normalized_website_url", "record_json"},
    "url_checks": {"url_check_id", "audit_run_id", "record_id", "classification", "http_status_code", "original_url", "final_url", "result_json"},
    "data_quality_issues": {"issue_id", "audit_run_id", "record_id", "category", "severity", "field", "message"},
}
_INVALID_URL_ISSUES = {"blank_website_url", "malformed_url", "unsupported_url_scheme"}


class AuditRunnerError(ValueError):
    """Raised when the target database cannot safely receive an audit run."""


@dataclass(frozen=True)
class AuditRunSummary:
    audit_run_id: str
    input_filename: str
    database_path: str
    total_rows: int
    valid_rows: int
    rows_with_issues: int
    issue_counts: dict[str, int]
    website_checks: int


def validate_existing_database(path: Path) -> None:
    """Allow appending only to a readable database with this project's schema."""
    if not path.exists():
        return
    if not path.is_file():
        raise AuditRunnerError("The database path exists but is not a file; it was left unchanged.")

    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise AuditRunnerError(
                    "The existing database has an unsupported schema version; it was left unchanged. "
                    "Choose a new path or a database created by this project."
                )
            for table, required_columns in _REQUIRED_TABLE_COLUMNS.items():
                existing_columns = {
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if not required_columns.issubset(existing_columns):
                    raise AuditRunnerError(
                        "The existing database is not a compatible Podcast Pipeline database; "
                        "it was left unchanged. Choose a new database path."
                    )
        finally:
            connection.close()
    except AuditRunnerError:
        raise
    except sqlite3.Error as exc:
        raise AuditRunnerError(
            "The existing database is unreadable or is not a compatible Podcast Pipeline database; "
            "it was left unchanged. Choose a new database path."
        ) from exc


def run_audit(
    input_path: str | Path = DEFAULT_INPUT_PATH,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    *,
    check_websites: bool = False,
    checker: Callable[[str], UrlCheckResult] | None = None,
) -> AuditRunSummary:
    """Ingest, validate, and persist one snapshot; URL requests are opt-in.

    By default, the audit is fully offline. Set ``check_websites=True`` only
    when you intend to request eligible HTTP(S) destinations from the input.
    Invalid or blank URLs are retained but never sent to the checker.
    """
    source_path = Path(input_path).expanduser()
    database_file = Path(database_path).expanduser()
    try:
        if source_path.resolve() == database_file.resolve():
            raise AuditRunnerError("The input and database paths must be different files.")
    except OSError as exc:
        raise AuditRunnerError("Could not resolve the input and database paths safely.") from exc

    # Preflight before processing or making any optional requests. Existing
    # non-project files are never initialized, truncated, or migrated here.
    validate_existing_database(database_file)
    imported = load_podcast_file(source_path)
    report: ValidationReport = validate_records(imported.records, imported.columns)

    url_results: dict[int, UrlCheckResult] = {}
    if check_websites:
        url_checker = checker or check_website_url
        for record in report.records:
            if any(issue.category in _INVALID_URL_ISSUES for issue in record.issues):
                continue
            candidate = record.normalized.get("normalized_website_url")
            if candidate:
                url_results[record.source_row] = url_checker(str(candidate))

    run_id = persist_audit_run(
        database_file,
        imported.source_filename,
        report.records,
        schema_issues=report.schema_issues,
        url_check_results=url_results,
        metadata={"url_checks_requested": check_websites},
    )
    return AuditRunSummary(
        audit_run_id=run_id,
        input_filename=imported.source_filename,
        database_path=str(database_file.resolve()),
        total_rows=report.total_rows,
        valid_rows=report.valid_rows,
        rows_with_issues=report.rows_with_issues,
        issue_counts=report.issue_counts,
        website_checks=len(url_results),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a podcast CSV/XLSX and append its audit snapshot to SQLite."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT_PATH), help="CSV or Excel input (default: data/sample_podcasts.csv)")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH), help="SQLite database path (default: data/podcast_audits.sqlite)")
    parser.add_argument(
        "--check-websites",
        action="store_true",
        help="Request eligible HTTP(S) URLs from the input; contacts the URL destinations. Omit for offline validation only.",
    )
    args = parser.parse_args(argv)
    try:
        summary = run_audit(args.input, args.database, check_websites=args.check_websites)
    except (AuditRunnerError, IngestionError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

