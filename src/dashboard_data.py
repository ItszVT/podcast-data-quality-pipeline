"""Read-only data access and transformations for the Streamlit dashboard."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests

from .database_config import DatabaseConfigurationError, require_sqlite_backend

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_DATABASE_PATH = PROJECT_ROOT / "data" / "real_podcast_audits.sqlite"
SYNTHETIC_DATABASE_PATH = PROJECT_ROOT / "data" / "podcast_audits.sqlite"
DEFAULT_DATABASE_PATH = REAL_DATABASE_PATH
SNAPSHOT_OWNER = "ItszVT"
SNAPSHOT_REPOSITORY = "podcast-data-quality-pipeline"
SNAPSHOT_BRANCH = "audit-snapshots"
SNAPSHOT_FILE = "data/real_podcast_audits.sqlite"
MAX_PUBLISHED_SNAPSHOT_BYTES = 50 * 1024 * 1024
MAX_DETAIL_ROWS = 5000

_REQUIRED_COLUMNS = {
    "audit_runs": {"audit_run_id", "started_at", "completed_at", "input_filename", "run_status", "records_processed"},
    "podcast_records": {"record_id", "audit_run_id", "source_row", "feed_id", "title", "website_url", "normalized_website_url", "record_json"},
    "data_quality_issues": {"issue_id", "audit_run_id", "record_id", "category", "severity", "field", "message"},
    "url_checks": {"url_check_id", "audit_run_id", "record_id", "classification", "http_status_code", "original_url", "final_url", "result_json"},
}
_ANALYTICS_QUERIES = {
    "audit_run_comparison.sql",
    "url_outcomes_over_time.sql",
    "run_record_counts.sql",
}


class DashboardDataError(RuntimeError):
    """Safe, user-facing database unavailable/schema error."""


def fetch_published_snapshot(token: str) -> bytes:
    """Fetch the versioned SQLite snapshot without exposing GitHub/API details."""
    if not isinstance(token, str) or not token.strip():
        raise DashboardDataError(
            "Published audit access is not configured. Add the read-only GitHub token to app secrets."
        )

    path = quote(SNAPSHOT_FILE, safe="/")
    url = f"https://api.github.com/repos/{SNAPSHOT_OWNER}/{SNAPSHOT_REPOSITORY}/contents/{path}"
    headers = {
        "Accept": "application/vnd.github.raw+json",
        "Authorization": f"Bearer {token.strip()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = None
    try:
        response = requests.get(
            url, headers=headers, params={"ref": SNAPSHOT_BRANCH},
            timeout=(5, 30), stream=True, allow_redirects=False,
        )
        if response.status_code == 404:
            raise DashboardDataError(
                "The published audit snapshot is unavailable. Run the audit workflow and check the read-only GitHub access setting."
            )
        if response.status_code != 200:
            raise DashboardDataError(
                "The published audit snapshot could not be read. Check the read-only GitHub access setting."
            )

        declared_size = response.headers.get("Content-Length")
        if declared_size:
            try:
                if int(declared_size) > MAX_PUBLISHED_SNAPSHOT_BYTES:
                    raise DashboardDataError("The published audit snapshot is larger than the supported limit.")
            except ValueError:
                pass

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_PUBLISHED_SNAPSHOT_BYTES:
                raise DashboardDataError("The published audit snapshot is larger than the supported limit.")
            chunks.append(chunk)
        snapshot = b"".join(chunks)
    except DashboardDataError:
        raise
    except requests.RequestException as exc:
        raise DashboardDataError(
            "The published audit snapshot could not be reached. Try refreshing later."
        ) from exc
    finally:
        if response is not None:
            response.close()

    if not snapshot.startswith(b"SQLite format 3\x00"):
        raise DashboardDataError("The published audit snapshot is missing or invalid.")
    return snapshot


@contextmanager
def _read_connection(database_path: str | Path | bytes) -> Iterator[sqlite3.Connection]:
    connection: sqlite3.Connection | None = None
    try:
        if isinstance(database_path, bytes):
            if not database_path.startswith(b"SQLite format 3\x00"):
                raise DashboardDataError("The published audit snapshot is missing or invalid.")
            connection = sqlite3.connect(":memory:")
            connection.deserialize(database_path)
        else:
            path = Path(database_path).expanduser()
            try:
                require_sqlite_backend(path)
            except DatabaseConfigurationError as exc:
                raise DashboardDataError(str(exc)) from exc
            if not path.is_file():
                raise DashboardDataError(
                    "Real audit database not found. Run an audit first; the dashboard will not create or substitute a database."
                )

            # A file URI in read-only mode prevents Streamlit interactions from
            # creating a missing DB or modifying stored audit history.
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        _verify_schema(connection)
        yield connection
    except DashboardDataError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise DashboardDataError(
            "Could not read the selected SQLite database. Check that it is a valid, readable project database."
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _verify_schema(connection: sqlite3.Connection) -> None:
    missing: list[str] = []
    for table, expected_columns in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not expected_columns.issubset(actual):
            missing.append(table)
    if missing:
        raise DashboardDataError(
            "The selected SQLite file does not have the expected Podcast Pipeline schema. "
            "Choose a database created by this project's audit persistence module."
        )


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_DETAIL_ROWS))


def _analytics_sql(filename: str) -> str:
    if filename not in _ANALYTICS_QUERIES:
        raise ValueError("Unsupported dashboard analytics query.")
    return (PROJECT_ROOT / "sql" / filename).read_text(encoding="utf-8-sig")


def get_runs(database_path: str | Path | bytes = DEFAULT_DATABASE_PATH) -> list[dict[str, Any]]:
    """Return available runs, newest first, without loading podcast rows."""
    with _read_connection(database_path) as connection:
        rows = _rows(connection.execute(
            """SELECT audit_run_id, input_filename, run_status, started_at,
                      completed_at, records_processed,
                      CASE WHEN json_valid(metadata_json)
                           THEN json_extract(metadata_json, '$.collection_report.source')
                           ELSE NULL END AS collection_source
               FROM audit_runs ORDER BY started_at DESC, audit_run_id DESC"""
        ))
        for row in rows:
            row["source_label"] = _source_label(row.get("input_filename"), row.pop("collection_source", None))
        return rows


def get_history_summary(database_path: str | Path | bytes = DEFAULT_DATABASE_PATH) -> dict[str, int]:
    """Return total run count and processed rows across all historical runs."""
    with _read_connection(database_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS audit_runs, COALESCE(SUM(records_processed), 0) AS records_processed FROM audit_runs"
        ).fetchone()
        return dict(row)


def get_run_summary(database_path: str | Path | bytes, audit_run_id: str) -> dict[str, Any] | None:
    """Return selected-run execution, quality, coverage, and URL summaries."""
    query = """
        SELECT
            ar.audit_run_id,
            ar.input_filename,
            ar.run_status,
            ar.started_at,
            ar.completed_at,
            ar.records_processed,
            ar.metadata_json,
            (SELECT COUNT(*) FROM podcast_records p
             WHERE p.audit_run_id = ar.audit_run_id) AS stored_records,
            (SELECT COUNT(*) FROM podcast_records p
             WHERE p.audit_run_id = ar.audit_run_id
               AND EXISTS (SELECT 1 FROM data_quality_issues i
                           WHERE i.audit_run_id = p.audit_run_id AND i.record_id = p.record_id))
                AS records_with_issues,
            (SELECT COUNT(*) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id) AS issue_occurrences,
            (SELECT COUNT(*) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id AND i.severity = 'error') AS actionable_issue_occurrences,
            (SELECT COUNT(DISTINCT i.record_id) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id AND i.severity = 'error'
               AND i.record_id IS NOT NULL) AS records_with_actionable_errors,
            (SELECT COUNT(*) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id AND i.category = 'missing_optional_field') AS completeness_issue_occurrences,
            (SELECT COUNT(*) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id AND i.severity = 'warning'
               AND i.category != 'missing_optional_field') AS other_warning_occurrences,
            (SELECT COUNT(*) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id AND i.severity = 'info') AS informational_issue_occurrences,
            (SELECT COUNT(DISTINCT i.record_id) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id AND i.category = 'missing_optional_field'
               AND i.record_id IS NOT NULL) AS records_with_completeness_notes,
            (SELECT COUNT(*) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id AND i.record_id IS NULL) AS schema_issue_count,
            (SELECT COUNT(*) FROM data_quality_issues i
             WHERE i.audit_run_id = ar.audit_run_id AND i.record_id IS NULL
               AND i.severity = 'error') AS file_level_error_count,
            (SELECT COUNT(*) FROM url_checks u
             WHERE u.audit_run_id = ar.audit_run_id) AS url_check_count,
            (SELECT COUNT(DISTINCT COALESCE(NULLIF(p.normalized_website_url, ''), NULLIF(u.original_url, '')))
             FROM url_checks u JOIN podcast_records p
               ON p.audit_run_id = u.audit_run_id AND p.record_id = u.record_id
             WHERE u.audit_run_id = ar.audit_run_id) AS url_unique_targets_saved,
            (SELECT COALESCE(SUM(CASE WHEN p.record_id IS NOT NULL AND
                                  (p.website_url IS NULL OR TRIM(p.website_url) = '')
                             THEN 1 ELSE 0 END), 0)
             FROM podcast_records p WHERE p.audit_run_id = ar.audit_run_id) AS missing_website_urls
        FROM audit_runs ar WHERE ar.audit_run_id = ?
    """
    with _read_connection(database_path) as connection:
        row = connection.execute(query, (audit_run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            metadata = json.loads(result.pop("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        collection_report = metadata.get("collection_report") if isinstance(metadata, dict) else None
        collection_report = collection_report if isinstance(collection_report, dict) else {}
        website = metadata.get("website_checking") if isinstance(metadata, dict) else None
        website = website if isinstance(website, dict) else {}
        website_status = website.get("status")
        if isinstance(website_status, str) and website_status in {"not_requested", "succeeded", "partial"}:
            result["website_check_status"] = website_status
        elif website.get("requested") is False or (metadata.get("url_checks_requested") is False if isinstance(metadata, dict) else False):
            result["website_check_status"] = "not_requested"
        else:
            # Older snapshots often did not persist this stage. Do not turn
            # missing evidence into a claim that checking was not requested.
            result["website_check_status"] = "unknown"
        result["website_check_unique_urls"] = (
            website.get("unique_urls_checked")
            if isinstance(website.get("unique_urls_checked"), int) and not isinstance(website.get("unique_urls_checked"), bool) and website.get("unique_urls_checked") >= 0
            else result["url_unique_targets_saved"] if result["url_check_count"] else None
        )
        result["website_unique_count_source"] = "pipeline_metadata" if isinstance(website.get("unique_urls_checked"), int) and not isinstance(website.get("unique_urls_checked"), bool) else "saved_results" if result["url_check_count"] else "unavailable"
        result["website_records_with_results"] = result["url_check_count"]
        result["website_check_unexpected_errors"] = _nonnegative_int(website.get("unexpected_error_count")) or 0
        stop_reason = website.get("stop_reason")
        result["website_check_stop_reason"] = stop_reason if isinstance(stop_reason, str) and stop_reason in {"unique_url_limit_reached", "stage_budget_exhausted", "checker_error"} else None
        rss_failures = collection_report.get("rss_retrieval_failures")
        result["rss_retrieval_failures"] = (
            rss_failures
            if isinstance(rss_failures, int) and not isinstance(rss_failures, bool) and rss_failures >= 0
            else None
        )
        result["rss_retrieval_successes"] = _nonnegative_int(collection_report.get("rss_retrieval_successes"))
        result["rss_feeds_missing_url"] = _nonnegative_int(collection_report.get("missing_rss_feed_urls"))
        result["execution_stages"] = _safe_stage_statuses(metadata.get("stages") if isinstance(metadata, dict) else None)
        result["source_label"] = _source_label(result.get("input_filename"), collection_report.get("source"))
        geo_metadata = metadata.get("geo_intelligence") if isinstance(metadata, dict) else None
        storefront = geo_metadata.get("storefront_context") if isinstance(geo_metadata, dict) else None
        result["storefront_label"] = (
            str(storefront.get("value")) if isinstance(storefront, dict) and storefront.get("value")
            else collection_report.get("storefront") if isinstance(collection_report.get("storefront"), str) and collection_report.get("storefront")
            else "Not recorded"
        )
        stored = result["stored_records"]
        result["records_with_issues_pct"] = (
            round(100.0 * result["records_with_issues"] / stored, 2) if stored else None
        )
        result["missing_website_url_pct"] = (
            round(100.0 * result["missing_website_urls"] / stored, 2) if stored else None
        )
        return result


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _safe_stage_statuses(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = {"succeeded", "partial", "failed", "skipped", "not_requested", "not_started"}
    return {str(name): status for name, status in value.items() if isinstance(name, str) and isinstance(status, str) and status in allowed}


def _source_label(input_filename: Any, source: Any) -> str:
    if isinstance(source, str) and source.strip() and "://" not in source:
        return source.strip()[:120]
    if isinstance(input_filename, str) and input_filename.strip():
        # Legacy runs may have stored a local path. Expose only its basename.
        return input_filename.strip().replace("\\", "/").rsplit("/", 1)[-1][:120]
    return "Source not recorded"


def get_geo_intelligence(database_path: str | Path | bytes, audit_run_id: str) -> dict[str, Any]:
    """Read the selected run's saved geo observations without network access."""
    with _read_connection(database_path) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM audit_runs WHERE audit_run_id = ?",
            (audit_run_id,),
        ).fetchone()
        if row is None:
            return {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        geo = metadata.get("geo_intelligence", {}) if isinstance(metadata, dict) else {}
        return geo if isinstance(geo, dict) else {}


def get_issue_counts(database_path: str | Path | bytes, audit_run_id: str) -> list[dict[str, Any]]:
    """Count validation issue occurrences by category and severity for a run."""
    query = """
        SELECT category, severity, COUNT(*) AS issue_count,
               COUNT(DISTINCT record_id) AS affected_record_count
        FROM data_quality_issues WHERE audit_run_id = ?
        GROUP BY category, severity
        ORDER BY issue_count DESC, category, severity
    """
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(query, (audit_run_id,)))


def get_issue_field_counts(database_path: str | Path | bytes, audit_run_id: str) -> list[dict[str, Any]]:
    """Count saved findings by category, field, and severity for plain-language summaries."""
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(
            """SELECT category, field, severity, message,
                      COUNT(*) AS issue_count,
                      COUNT(DISTINCT record_id) AS affected_record_count
               FROM data_quality_issues WHERE audit_run_id = ?
               GROUP BY category, field, severity, message
               ORDER BY issue_count DESC, category, field""",
            (audit_run_id,),
        ))


def get_podcast_records(
    database_path: str | Path | bytes,
    audit_run_id: str,
    *,
    filter_mode: str = "all",
    issue_category: str | None = None,
    search: str = "",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return a bounded, filterable record list with source metadata and finding counts."""
    allowed = {"all", "with_findings", "without_findings", "with_actionable_errors", "with_completeness_notes", "missing_website", "category"}
    if filter_mode not in allowed:
        raise ValueError(f"Unsupported podcast record filter: {filter_mode}")
    clauses = ["p.audit_run_id = ?"]
    params: list[Any] = [audit_run_id]
    issue_exists = "EXISTS (SELECT 1 FROM data_quality_issues i WHERE i.audit_run_id=p.audit_run_id AND i.record_id=p.record_id)"
    if filter_mode == "with_findings":
        clauses.append(issue_exists)
    elif filter_mode == "with_actionable_errors":
        clauses.append("EXISTS (SELECT 1 FROM data_quality_issues i WHERE i.audit_run_id=p.audit_run_id AND i.record_id=p.record_id AND i.severity='error')")
    elif filter_mode == "with_completeness_notes":
        clauses.append("EXISTS (SELECT 1 FROM data_quality_issues i WHERE i.audit_run_id=p.audit_run_id AND i.record_id=p.record_id AND i.severity='warning')")
    elif filter_mode == "without_findings":
        clauses.append(f"NOT ({issue_exists})")
    elif filter_mode == "missing_website":
        clauses.append("(p.website_url IS NULL OR TRIM(p.website_url) = '')")
    elif filter_mode == "category":
        if not issue_category:
            raise ValueError("Choose a finding category for this filter.")
        clauses.append("EXISTS (SELECT 1 FROM data_quality_issues i WHERE i.audit_run_id=p.audit_run_id AND i.record_id=p.record_id AND i.category=?)")
        params.append(issue_category)
    if search.strip():
        pattern = f"%{search.strip()}%"
        clauses.append("(COALESCE(p.feed_id,'') LIKE ? OR COALESCE(p.title,'') LIKE ? OR COALESCE(p.website_url,'') LIKE ?)")
        params.extend((pattern, pattern, pattern))
    params.append(_limit(limit))
    query = f"""
        SELECT p.record_id, p.source_row, p.feed_id, p.title, p.website_url,
               json_extract(p.record_json, '$.original.description') AS description,
               json_extract(p.record_json, '$.original.categories') AS categories,
               json_extract(p.record_json, '$.original.language') AS language,
               json_extract(p.record_json, '$.original.country') AS country,
               (SELECT COUNT(*) FROM data_quality_issues i
                WHERE i.audit_run_id=p.audit_run_id AND i.record_id=p.record_id) AS finding_count
        FROM podcast_records p
        WHERE {' AND '.join(clauses)}
        ORDER BY p.source_row LIMIT ?
    """
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(query, params))


def get_record_findings(database_path: str | Path | bytes, audit_run_id: str, record_id: int) -> list[dict[str, Any]]:
    """Return the saved findings for one record, without exposing raw JSON."""
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(
            """SELECT category, severity, field, message
               FROM data_quality_issues
               WHERE audit_run_id=? AND record_id=?
               ORDER BY severity, category, field""",
            (audit_run_id, record_id),
        ))


def get_record_url_check(database_path: str | Path | bytes, audit_run_id: str, record_id: int) -> dict[str, Any] | None:
    """Return one saved URL observation only when it belongs to this run and record."""
    with _read_connection(database_path) as connection:
        row = connection.execute(
            """SELECT classification, http_status_code, original_url, final_url,
                      result_json
               FROM url_checks
               WHERE audit_run_id=? AND record_id=?""",
            (audit_run_id, record_id),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            details = json.loads(result.pop("result_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        result.update({
            "redirect_chain": details.get("redirect_chain", []) if isinstance(details, dict) else [],
            "evidence": details.get("evidence") if isinstance(details, dict) else None,
            "error_reason": details.get("error_reason") if isinstance(details, dict) else None,
        })
        return result


def get_schema_issues(database_path: str | Path | bytes, audit_run_id: str) -> list[dict[str, Any]]:
    """Return file-level validation issues not associated with an input row."""
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(
            """SELECT category, severity, field, message FROM data_quality_issues
               WHERE audit_run_id = ? AND record_id IS NULL
               ORDER BY category, field""",
            (audit_run_id,),
        ))


def get_affected_records(
    database_path: str | Path | bytes,
    audit_run_id: str,
    *,
    issue_category: str | None = None,
    issue_field: str | None = None,
    search: str = "",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return filterable per-record issue details; a row appears only once."""
    clauses = ["p.audit_run_id = ?", "i.record_id IS NOT NULL"]
    params: list[Any] = [audit_run_id]
    if issue_category:
        clauses.append("i.category = ?")
        params.append(issue_category)
    if issue_field:
        clauses.append("i.field = ?")
        params.append(issue_field)
    if search.strip():
        clauses.append("(COALESCE(p.feed_id, '') LIKE ? OR COALESCE(p.title, '') LIKE ? OR COALESCE(p.website_url, '') LIKE ?)")
        pattern = f"%{search.strip()}%"
        params.extend((pattern, pattern, pattern))
    params.append(_limit(limit))
    query = f"""
        SELECT p.record_id, p.source_row, p.feed_id, p.title, p.website_url,
               p.normalized_website_url,
               json_extract(p.record_json, '$.original.description') AS description,
               json_extract(p.record_json, '$.original.categories') AS categories,
               json_extract(p.record_json, '$.original.language') AS language,
               json_extract(p.record_json, '$.original.country') AS country,
               COUNT(i.issue_id) AS issue_count,
               GROUP_CONCAT(DISTINCT i.category) AS issue_categories,
               GROUP_CONCAT(i.category || ' (' || i.severity || '): ' || i.message, ' | ') AS issue_details
        FROM podcast_records p
        JOIN data_quality_issues i
          ON i.audit_run_id = p.audit_run_id AND i.record_id = p.record_id
        WHERE {' AND '.join(clauses)}
        GROUP BY p.record_id
        ORDER BY p.source_row
        LIMIT ?
    """
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(query, params))


def get_duplicate_feed_ids(database_path: str | Path | bytes, audit_run_id: str) -> list[dict[str, Any]]:
    """Return duplicate nonblank normalized identifiers within one run."""
    identifier = "COALESCE(NULLIF(json_extract(record_json, '$.normalized.feed_id'), ''), NULLIF(TRIM(feed_id), ''))"
    query = f"""
        SELECT {identifier} AS feed_id, COUNT(*) AS record_count,
               GROUP_CONCAT(source_row, ', ') AS source_rows
        FROM podcast_records
        WHERE audit_run_id = ? AND {identifier} IS NOT NULL
        GROUP BY {identifier}
        HAVING COUNT(*) > 1
        ORDER BY record_count DESC, feed_id
    """
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(query, (audit_run_id,)))


def get_issue_categories(database_path: str | Path | bytes, audit_run_id: str) -> list[str]:
    with _read_connection(database_path) as connection:
        return [row[0] for row in connection.execute(
            """SELECT DISTINCT category FROM data_quality_issues
               WHERE audit_run_id = ? AND record_id IS NOT NULL ORDER BY category""",
            (audit_run_id,),
        )]


def get_url_outcomes(database_path: str | Path | bytes, audit_run_id: str) -> list[dict[str, Any]]:
    query = """
        SELECT classification, COUNT(*) AS record_count,
               ROUND(100.0 * COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (), 0), 2) AS checked_pct
        FROM url_checks WHERE audit_run_id = ?
        GROUP BY classification ORDER BY record_count DESC, classification
    """
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(query, (audit_run_id,)))


def get_http_status_distribution(database_path: str | Path | bytes, audit_run_id: str) -> list[dict[str, Any]]:
    query = """
        SELECT COALESCE(CAST(http_status_code AS TEXT), 'no_http_response') AS status_bucket,
               COUNT(*) AS check_count,
               ROUND(100.0 * COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (), 0), 2) AS checked_pct
        FROM url_checks WHERE audit_run_id = ?
        GROUP BY status_bucket ORDER BY check_count DESC, status_bucket
    """
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(query, (audit_run_id,)))


def get_redirect_summary(database_path: str | Path | bytes, audit_run_id: str) -> dict[str, Any]:
    query = """
        SELECT COUNT(*) AS checked_records,
               COALESCE(SUM(CASE WHEN COALESCE(json_array_length(result_json, '$.redirect_chain'), 0) > 0
                        THEN 1 ELSE 0 END), 0) AS records_with_redirects,
               COALESCE(SUM(COALESCE(json_array_length(result_json, '$.redirect_chain'), 0)), 0)
                   AS redirect_hops,
               AVG(COALESCE(json_array_length(result_json, '$.redirect_chain'), 0))
                   AS avg_redirect_hops
        FROM url_checks WHERE audit_run_id = ?
    """
    with _read_connection(database_path) as connection:
        return dict(connection.execute(query, (audit_run_id,)).fetchone())


def get_redirect_destinations(database_path: str | Path | bytes, audit_run_id: str) -> list[dict[str, Any]]:
    query = """
        SELECT json_extract(hop.value, '$.destination_url') AS destination_url,
               COUNT(*) AS observed_hops
        FROM url_checks u, json_each(u.result_json, '$.redirect_chain') AS hop
        WHERE u.audit_run_id = ?
        GROUP BY destination_url ORDER BY observed_hops DESC, destination_url
    """
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(query, (audit_run_id,)))


def get_url_check_rows(
    database_path: str | Path | bytes,
    audit_run_id: str,
    *,
    classification: str | None = None,
    search: str = "",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    clauses = ["u.audit_run_id = ?"]
    params: list[Any] = [audit_run_id]
    if classification:
        clauses.append("u.classification = ?")
        params.append(classification)
    if search.strip():
        clauses.append("(COALESCE(u.original_url, '') LIKE ? OR COALESCE(u.final_url, '') LIKE ? OR COALESCE(p.title, '') LIKE ? OR COALESCE(p.feed_id, '') LIKE ?)")
        pattern = f"%{search.strip()}%"
        params.extend((pattern, pattern, pattern, pattern))
    params.append(_limit(limit))
    query = f"""
        SELECT p.source_row, p.feed_id, p.title,
               u.original_url, u.final_url, u.classification, u.http_status_code,
               json_array_length(u.result_json, '$.redirect_chain') AS redirect_count,
               json_extract(u.result_json, '$.page_title') AS page_title,
               json_extract(u.result_json, '$.evidence') AS evidence,
               json_extract(u.result_json, '$.error_reason') AS error_reason
        FROM url_checks u
        JOIN podcast_records p
          ON p.audit_run_id = u.audit_run_id AND p.record_id = u.record_id
        WHERE {' AND '.join(clauses)}
        ORDER BY p.source_row
        LIMIT ?
    """
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(query, params))


def get_history_metrics(database_path: str | Path | bytes) -> list[dict[str, Any]]:
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(_analytics_sql("audit_run_comparison.sql")))


def get_url_outcome_trends(database_path: str | Path | bytes) -> list[dict[str, Any]]:
    with _read_connection(database_path) as connection:
        return _rows(connection.execute(_analytics_sql("url_outcomes_over_time.sql")))


def _comparison_record(row: sqlite3.Row) -> dict[str, Any]:
    try:
        raw = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    normalized = raw.get("normalized") or {}
    stable_id = normalized.get("feed_id")
    if stable_id is None:
        stable_id = (row["feed_id"] or "").strip()
    stable_id = str(stable_id).strip() if stable_id is not None else ""
    return {
        "record_id": row["record_id"],
        "source_row": row["source_row"],
        "feed_id": stable_id or None,
        "title": row["title"],
    }


def compare_runs(
    database_path: str | Path | bytes,
    earlier_run_id: str,
    later_run_id: str,
) -> dict[str, Any]:
    """Compare two runs using only nonblank IDs unique in both snapshots.

    Repeated IDs in either run are returned as ambiguous. Rows without stable IDs
    are returned as unidentified. URL outcomes are compared only when both matched
    records have stored URL checks.
    """
    if earlier_run_id == later_run_id:
        raise ValueError("Choose two different audit runs to compare.")
    with _read_connection(database_path) as connection:
        runs = connection.execute(
            "SELECT audit_run_id FROM audit_runs WHERE audit_run_id IN (?, ?)",
            (earlier_run_id, later_run_id),
        ).fetchall()
        if len(runs) != 2:
            raise ValueError("Both selected audit runs must exist in the database.")

        def load_records(run_id: str) -> list[dict[str, Any]]:
            rows = connection.execute(
                """SELECT record_id, source_row, feed_id, title, record_json
                   FROM podcast_records WHERE audit_run_id = ? ORDER BY source_row""",
                (run_id,),
            ).fetchall()
            return [_comparison_record(row) for row in rows]

        earlier_records = load_records(earlier_run_id)
        later_records = load_records(later_run_id)
        checks: dict[str, dict[int, str]] = {}
        for run_id in (earlier_run_id, later_run_id):
            checks[run_id] = {
                row["record_id"]: row["classification"]
                for row in connection.execute(
                    "SELECT record_id, classification FROM url_checks WHERE audit_run_id = ?",
                    (run_id,),
                )
            }

    def group(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            stable_id = record["feed_id"]
            if stable_id:
                grouped.setdefault(stable_id, []).append(record)
        return grouped

    earlier_by_id, later_by_id = group(earlier_records), group(later_records)
    present: list[dict[str, Any]] = []
    newly_observed: list[dict[str, Any]] = []
    absent_later: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for stable_id in sorted(set(earlier_by_id) | set(later_by_id)):
        previous = earlier_by_id.get(stable_id, [])
        current = later_by_id.get(stable_id, [])
        if len(previous) > 1 or len(current) > 1:
            ambiguous.append({"feed_id": stable_id, "earlier_records": previous, "later_records": current})
        elif previous and current:
            previous, current = previous[0], current[0]
            previous_outcome = checks[earlier_run_id].get(previous["record_id"])
            current_outcome = checks[later_run_id].get(current["record_id"])
            if previous_outcome is None or current_outcome is None:
                outcome_status = "not_checked_in_both"
                changed = None
            else:
                changed = previous_outcome != current_outcome
                outcome_status = "changed" if changed else "unchanged"
            present.append({
                "feed_id": stable_id,
                "earlier": previous,
                "later": current,
                "earlier_url_classification": previous_outcome,
                "later_url_classification": current_outcome,
                "url_outcome_status": outcome_status,
                "url_outcome_changed": changed,
            })
        elif current:
            newly_observed.append(current[0])
        elif previous:
            absent_later.append(previous[0])

    unidentified = [
        {"run": "earlier", **record} for record in earlier_records if not record["feed_id"]
    ] + [
        {"run": "later", **record} for record in later_records if not record["feed_id"]
    ]
    return {
        "earlier_run_id": earlier_run_id,
        "later_run_id": later_run_id,
        "present_in_both": present,
        "newly_observed": newly_observed,
        "absent_from_later": absent_later,
        "ambiguous_identifiers": ambiguous,
        "unidentified_records": unidentified,
    }

