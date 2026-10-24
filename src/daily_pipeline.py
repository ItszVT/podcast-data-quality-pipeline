"""Compose the existing collector, validator, and SQLite store for one daily run."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .audit_runner import AuditRunnerError, validate_existing_database
from .database import persist_audit_run
from .database_config import require_sqlite_backend
from .geo_intelligence import build_geo_intelligence, classify_episode_languages
from .podcast_collector import SEARCH_TERMS, CollectionResult, CollectorConfig, collect_podcasts
from .validation import ValidationReport, validate_records
from .url_checker import INCONCLUSIVE, UrlCheckResult, UrlCheckerConfig, check_website_url

DEFAULT_DATABASE_PATH = Path("data/real_podcast_audits.sqlite")
INPUT_DESCRIPTION = "Apple iTunes Search API collection"
INPUT_COLUMNS = ("feed_id", "title", "website_url", "description", "categories", "language", "country")


class PipelinePreflightError(ValueError):
    """A run could not start; carries the generated ID for an actionable report."""

    def __init__(self, audit_run_id: str, started_at: str, message: str):
        super().__init__(message)
        self.audit_run_id = audit_run_id
        self.started_at = started_at


class PipelineRunAlreadyExists(PipelinePreflightError):
    """Raised before collection when a supplied id already has a saved run."""


@dataclass(frozen=True)
class PipelineSummary:
    audit_run_id: str
    preflight_status: str
    started_at: str
    completed_at: str
    pipeline_status: str
    collection_status: str
    validation_status: str
    persistence_status: str
    database_path: str
    records_collected: int
    records_validated: int
    records_stored: int
    records_with_findings: int
    finding_occurrences: int
    issue_counts: dict[str, int]
    stage_failures: tuple[str, ...]
    website_check_status: str = "not_requested"
    website_check_unique_urls: int = 0
    website_check_records: int = 0


def _run_exists(database_path: Path, run_id: str) -> bool:
    """Check a compatible database read-only, without creating it."""
    if not database_path.is_file():
        return False
    uri = database_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        return connection.execute(
            "SELECT 1 FROM audit_runs WHERE audit_run_id = ? LIMIT 1", (run_id,)
        ).fetchone() is not None


def _raw_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain collected source values if validation raises unexpectedly."""
    return [
        {"source_row": index + 2, "original": dict(record),
         "normalized": dict(record), "issues": ()}
        for index, record in enumerate(records)
    ]


def _collection_status(result: CollectionResult) -> str:
    report = result.report
    if report.get("api_search_errors") or report.get("rss_retrieval_failures", 0):
        return "partial"
    return "succeeded"


def run_daily_pipeline(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    *,
    config: CollectorConfig | None = None,
    search_terms: Sequence[str] | None = None,
    audit_run_id: str | None = None,
    collector: Callable[..., CollectionResult] | None = None,
    validator: Callable[..., ValidationReport] | None = None,
    persister: Callable[..., str] | None = None,
    check_websites: bool = False,
    website_checker: Callable[..., UrlCheckResult] | None = None,
    website_checker_config: UrlCheckerConfig | None = None,
) -> PipelineSummary:
    """Collect, validate, and persist a snapshot; findings are not run failures.

    A supplied audit_run_id is an idempotency key. If it already exists,
    the call is refused before collection can make any network requests.
    """
    database_file = Path(database_path).expanduser()
    run_id = audit_run_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    # Reuse the audit runner's existing read-only database compatibility check.
    try:
        require_sqlite_backend(database_file)
        validate_existing_database(database_file)
        if audit_run_id and _run_exists(database_file, run_id):
            raise PipelineRunAlreadyExists(
                run_id,
                started_at,
                f"Audit run ID '{run_id}' already exists in '{database_file}'. "
                "Choose a new ID for a new snapshot.",
            )
    except PipelinePreflightError:
        raise
    except (AuditRunnerError, OSError, sqlite3.Error, ValueError) as exc:
        raise PipelinePreflightError(run_id, started_at, str(exc)) from exc

    collect_fn = collector or collect_podcasts
    validate_fn = validator or validate_records
    persist_fn = persister or persist_audit_run
    settings = config or CollectorConfig()
    terms = tuple(search_terms) if search_terms is not None else SEARCH_TERMS

    collection_status = "failed"
    validation_status = "skipped"
    persistence_status = "failed"
    collected: tuple[dict[str, str], ...] = ()
    collection_report: dict[str, Any] = {}
    validation_report: ValidationReport | None = None
    records_to_store: Sequence[Any] = ()
    schema_issues: Sequence[Any] = ()
    issue_counts: dict[str, int] = {}
    records_with_findings = finding_occurrences = 0
    failures: list[str] = []
    url_check_results: dict[int, UrlCheckResult] = {}
    website_status = "not_requested"
    website_unique_count = 0
    website_stop_reason: str | None = None
    website_unexpected_errors = 0

    try:
        result = collect_fn(settings, search_terms=terms)
        collected = result.records
        collection_report = result.report
        collection_status = _collection_status(result)
        if collection_status == "partial":
            api_errors = result.report.get("api_search_errors") or []
            rss_errors = result.report.get("rss_errors") or []
            if api_errors:
                failures.append("collection partial (API search): " + "; ".join(str(item) for item in api_errors))
            if rss_errors:
                failures.append("collection partial (RSS metadata): " + "; ".join(str(item) for item in rss_errors))
            elif result.report.get("rss_retrieval_failures", 0):
                failures.append("collection partial (RSS metadata): {} feed(s) failed".format(result.report["rss_retrieval_failures"]))
    except Exception as exc:
        failures.append(f"collection: {type(exc).__name__}: {exc}")

    if collection_status != "failed":
        try:
            validation_report = validate_fn(collected, columns=INPUT_COLUMNS)
            validation_status = "succeeded"
            records_to_store = validation_report.records
            schema_issues = validation_report.schema_issues
            issue_counts = validation_report.issue_counts
            records_with_findings = validation_report.rows_with_issues
            finding_occurrences = sum(len(row.issues) for row in validation_report.records)
            finding_occurrences += len(validation_report.schema_issues)
        except Exception as exc:
            validation_status = "failed"
            records_to_store = _raw_records(collected)
            failures.append(f"validation: {type(exc).__name__}: {exc}")

    if collection_status == "failed":
        records_to_store = ()

    if check_websites and validation_status == "succeeded" and collection_status != "failed":
        website_status = "succeeded"
        checker = website_checker or check_website_url
        check_settings = website_checker_config or UrlCheckerConfig()
        deadline = time.monotonic() + min(check_settings.stage_budget_seconds, 90.0)
        candidates: dict[str, list[int]] = {}
        for row in validation_report.records if validation_report else ():
            if any(issue.category in {"blank_website_url", "malformed_url", "unsupported_url_scheme"} for issue in row.issues):
                continue
            candidate = row.normalized.get("normalized_website_url")
            if candidate:
                candidates.setdefault(str(candidate), []).append(row.source_row)
        for candidate, source_rows in candidates.items():
            if website_unique_count >= 20:
                website_status, website_stop_reason = "partial", "unique_url_limit_reached"
                break
            if time.monotonic() >= deadline:
                website_status, website_stop_reason = "partial", "stage_budget_exhausted"
                break
            try:
                result = checker(candidate, check_settings, deadline_monotonic=deadline)
                if not isinstance(result, UrlCheckResult):
                    raise TypeError
            except Exception:
                # Checker exceptions may embed URLs or environment details.
                # Persist only a fixed category and fixed evidence.
                website_unexpected_errors += 1
                website_status = "partial"
                result = UrlCheckResult(
                    original_url=candidate, final_url=None, http_status_code=None,
                    redirect_count=0, redirect_chain=(), response_time_ms=None,
                    classification=INCONCLUSIVE,
                    evidence="The website check could not be completed safely.",
                    error_reason="checker_error",
                )
            website_unique_count += 1
            for source_row in source_rows:
                url_check_results[source_row] = result
        if website_unexpected_errors and website_stop_reason is None:
            website_stop_reason = "checker_error"

    run_status = (
        "failed" if collection_status == "failed" or validation_status == "failed"
        else "partial" if collection_status == "partial"
        else "succeeded"
    )
    completed_at = datetime.now(timezone.utc).isoformat()
    # Analyze bounded RSS episode text in memory. Persist only language labels,
    # counts, and method evidence; never copy episode text into the audit JSON.
    episode_samples = collection_report.pop("episode_text_samples_by_feed", {})
    if isinstance(episode_samples, dict):
        collection_report["episode_language_evidence"] = {
            str(feed_id): classify_episode_languages(samples)
            for feed_id, samples in episode_samples.items()
            if isinstance(samples, (list, tuple))
        }
    geo_intelligence = build_geo_intelligence(
        database_file, collected, collection_report, completed_at
    )
    metadata = {
        "pipeline": "daily_collection",
        "stages": {
            "preflight": "succeeded",
            "collection": collection_status,
            "validation": validation_status,
            # The row itself is atomic: if this transaction commits, the saved
            # audit proves persistence succeeded; if not, no run row is left.
            "persistence": "succeeded",
            "website_checking": website_status,
        },
        "collection_report": collection_report,
        "geo_intelligence": geo_intelligence,
        "records_collected": len(collected),
        "records_validated": validation_report.total_rows if validation_report else 0,
        "records_with_findings": records_with_findings,
        "finding_occurrences": finding_occurrences,
        "issue_counts": issue_counts,
        "website_checking": {
            "requested": check_websites,
            "status": website_status,
            "unique_urls_checked": website_unique_count,
            "records_with_results": len(url_check_results),
            "unexpected_error_count": website_unexpected_errors,
            "stop_reason": website_stop_reason,
        },
    }

    try:
        persist_fn(
            database_file, INPUT_DESCRIPTION, records_to_store,
            audit_run_id=run_id, started_at=started_at, completed_at=completed_at,
            run_status=run_status, error_summary="; ".join(failures) or None,
            metadata=metadata, schema_issues=schema_issues,
            url_check_results=url_check_results,
        )
        persistence_status = "succeeded"
        records_stored = len(records_to_store)
    except Exception as exc:
        persistence_status = "failed"
        records_stored = 0
        failures.append(f"persistence: {type(exc).__name__}: {exc}")

    pipeline_status = (
        "failed" if collection_status == "failed" or validation_status == "failed" or persistence_status == "failed"
        else "partial" if collection_status == "partial"
        else "succeeded"
    )
    return PipelineSummary(
        audit_run_id=run_id, started_at=started_at, completed_at=completed_at,
        pipeline_status=pipeline_status, preflight_status="succeeded",
        collection_status=collection_status, validation_status=validation_status,
        persistence_status=persistence_status,
        database_path=str(database_file.resolve()), records_collected=len(collected),
        records_validated=validation_report.total_rows if validation_report else 0,
        records_stored=records_stored, records_with_findings=records_with_findings,
        finding_occurrences=finding_occurrences, issue_counts=issue_counts,
        stage_failures=tuple(failures),
        website_check_status=website_status,
        website_check_unique_urls=website_unique_count,
        website_check_records=len(url_check_results),
    )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _nonnegative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect, validate, and save one Podcast Pipeline audit snapshot."
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH),
                        help="SQLite audit database (default: data/real_podcast_audits.sqlite)")
    parser.add_argument("--run-id", help="Optional idempotency key; an existing ID is refused before collection.")
    parser.add_argument("--limit", type=_positive_int, default=50, help="Maximum unique podcasts to retain.")
    parser.add_argument("--results-per-term", type=_positive_int, default=10)
    parser.add_argument("--search-term", action="append", dest="search_terms",
                        help="Search term; repeat to set multiple terms (defaults to the collector's terms).")
    parser.add_argument("--connect-timeout-seconds", type=_positive_float, default=4.0)
    parser.add_argument("--read-timeout-seconds", type=_positive_float, default=7.0)
    parser.add_argument("--max-response-bytes", type=_positive_int, default=2_000_000)
    parser.add_argument("--max-redirects", type=_nonnegative_int, default=3)
    parser.add_argument("--api-pacing-seconds", type=_nonnegative_float, default=3.0)
    parser.add_argument("--rss-pacing-seconds", type=_nonnegative_float, default=0.4)
    parser.add_argument("--check-websites", action="store_true", help="Check up to 20 unique validated website URLs (bounded network requests).")
    args = parser.parse_args(argv)
    if args.search_terms and any(not term.strip() for term in args.search_terms):
        parser.error("--search-term values must not be blank")

    config = CollectorConfig(
        max_records=args.limit, results_per_term=args.results_per_term,
        connect_timeout_seconds=args.connect_timeout_seconds,
        read_timeout_seconds=args.read_timeout_seconds,
        max_response_bytes=args.max_response_bytes, max_redirects=args.max_redirects,
        api_pacing_seconds=args.api_pacing_seconds, rss_pacing_seconds=args.rss_pacing_seconds,
    )
    try:
        summary = run_daily_pipeline(
            args.database, config=config, search_terms=args.search_terms, audit_run_id=args.run_id,
            check_websites=args.check_websites,
        )
    except PipelinePreflightError as exc:
        print(json.dumps({
            "audit_run_id": exc.audit_run_id,
            "started_at": exc.started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "pipeline_status": "failed",
            "preflight_status": "failed",
            "collection_status": "not_started",
            "validation_status": "not_started",
            "persistence_status": "not_started",
            "new_audit_record_written": False,
            "stage_failures": [f"preflight: {exc}"],
        }, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
    return {"succeeded": 0, "partial": 2, "failed": 1}[summary.pipeline_status]


if __name__ == "__main__":
    sys.exit(main())





